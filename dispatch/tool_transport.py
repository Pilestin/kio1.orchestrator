"""Transports for targets that are not KIO agents.

A KIO agent knows the KIO1-KIO10 protocol: it accepts an execution message
and answers in the agreed shape. Most useful things do not. An internal REST
API has its own request body; a command line tool has arguments, an exit
code, and text on two streams.

These transports adapt both directions. Outbound, the execution message is
substituted into whatever the target expects. Inbound, whatever the target
answers is turned back into the reply shape the executor already reads, so
`workflow_executor` treats a `pytest` run and a KIO agent identically.
"""

import json
import locale
import logging
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import replace
from typing import Any
from urllib.parse import quote

from opentelemetry.propagate import inject

from dispatch.agent_registry import AgentEndpoint, CliToolSpec, HttpToolSpec
from dispatch.agent_transport import AgentTransportError
from dispatch.workflow_planner import ExecutionMessage

logger = logging.getLogger(__name__)

_MAX_CAPTURED_CHARS = 8000

# `{name}` and `{data.some.path}`. Anything else keeping braces is left alone,
# because a configured body may legitimately contain them.
_PLACEHOLDER = re.compile(r"\{([a-z_]+(?:\.[A-Za-z0-9_-]+)*)\}")

# `${NAME}`, so a token lives in the environment rather than in config.json.
_ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _lookup(name: str, message: ExecutionMessage) -> Any:
    """Resolve one placeholder name against an execution message.

    Args:
        name: The placeholder body, such as `task` or `data.repository.path`.
        message: The message being dispatched.

    Returns:
        The resolved value, or `None` when the name is not a known field. A
        caller that gets `None` leaves the placeholder text untouched rather
        than substituting an empty string, so a typo is visible in the request
        instead of silently blanking a field.
    """
    scalars = {
        "task": message.task,
        "workflow_id": message.workflow_id,
        "step_id": message.step_id,
        "agent_id": message.agent_id,
        "capability": message.capability,
        "endpoint": message.endpoint,
    }

    if name in scalars:
        return scalars[name]

    if name == "data":
        return message.data

    # `{arguments.x}` is shorthand for `{data.arguments.x}`: the arguments a
    # plan step supplies are what a configured URL or body most often wants.
    if name == "arguments" or name.startswith("arguments."):
        name = f"data.{name}"

    if not name.startswith("data."):
        return None

    current: Any = message.data

    for key in name.split(".")[1:]:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]

    return current


def render_url(url: str, message: ExecutionMessage) -> str:
    """Substitute fields into a URL, percent-encoding what is substituted.

    A task such as "Weather in Istanbul" in a query string must arrive as
    `Weather%20in%20Istanbul`: unencoded, the space makes the request invalid,
    and an `&` in it would start a parameter nobody configured.

    Args:
        url: The configured URL.
        message: The message being dispatched.

    Returns:
        The URL with placeholders resolved and encoded. A URL that is exactly
        one placeholder is taken as a whole address and left as it is.
    """
    return str(render(url, message, encode=lambda text: quote(text, safe="")))


def render(
    value: Any,
    message: ExecutionMessage,
    *,
    encode: Callable[[str], str] | None = None,
) -> Any:
    """Substitute execution-message fields into a configured value.

    A string that is *exactly* one placeholder is replaced by the raw value,
    so `"{data}"` yields an object rather than its printed form. A placeholder
    inside a longer string is replaced by its text.

    Args:
        value: A configured string, list, or object.
        message: The message being dispatched.
        encode: Applied to text substituted inside a longer string, such as
            URL encoding. Omitted inserts it unchanged.

    Returns:
        The value with placeholders resolved. Lists and objects are walked.
    """
    if isinstance(value, dict):
        return {
            key: render(item, message, encode=encode) for key, item in value.items()
        }

    if isinstance(value, list):
        return [render(item, message, encode=encode) for item in value]

    if not isinstance(value, str):
        return value

    whole = _PLACEHOLDER.fullmatch(value)
    if whole is not None:
        resolved = _lookup(whole.group(1), message)
        return value if resolved is None else resolved

    def substitute(match: re.Match[str]) -> str:
        resolved = _lookup(match.group(1), message)

        if resolved is None:
            return match.group(0)

        text = (
            resolved
            if isinstance(resolved, str)
            else json.dumps(resolved, ensure_ascii=False)
        )

        return encode(text) if encode is not None else text

    return _PLACEHOLDER.sub(substitute, value)


def expand_environment(value: str, *, agent_id: str, field: str) -> str:
    """Replace `${NAME}` references with values from the environment.

    Args:
        value: The configured string.
        agent_id: Identifier of the entry, for error messages.
        field: Where the value came from, for error messages.

    Returns:
        The string with every reference expanded.

    Raises:
        AgentTransportError: If a referenced variable is unset. Sending the
            request without it would mean calling an authenticated API with an
            empty credential and reading the rejection as a tool failure.
    """
    missing: list[str] = []

    def substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        resolved = os.environ.get(name)

        if resolved is None:
            missing.append(name)
            return ""

        return resolved

    expanded = _ENV_REFERENCE.sub(substitute, value)

    if missing:
        raise AgentTransportError(
            f"{agent_id} needs environment variable(s) {', '.join(missing)} "
            f"for its {field}, and they are not set."
        )

    return expanded


def protocol_reply(
    message: ExecutionMessage,
    *,
    status: str,
    output: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Build the protocol reply the executor reads.

    Args:
        message: The message this is answering.
        status: `ok` or `error`.
        output: The result object, when the call succeeded.
        error: The reason, when it did not.

    Returns:
        A reply in the KIO1-KIO10 shape.
    """
    return {
        "workflow_id": message.workflow_id,
        "step_id": message.step_id,
        "agent_id": message.agent_id,
        "status": status,
        "output": output if output is not None else {},
        "error": error,
    }


def decode_output(data: bytes | None) -> str:
    """Read what a program printed, whichever encoding it printed in.

    Git and most modern tools write UTF-8 whatever the system says; a Python
    script or a Windows tool writes the console's code page. Reading all of
    it as the code page turned every "—" in a commit message into "â€"", so
    UTF-8 is tried first — text in a code page is almost never valid UTF-8 —
    and the system's encoding is the fallback.

    Args:
        data: The captured bytes, or None when nothing was captured.

    Returns:
        The text, with anything undecodable replaced rather than raised.
    """
    if not data:
        return ""

    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode(locale.getpreferredencoding(False), errors="replace")


def _object_in_text(text: str) -> Any:
    """Return the JSON object a string holds, or the string unchanged.

    A model served over HTTP (Ollama's ``message.content``, OpenAI's
    ``choices[0].message.content``) returns its structured answer as text.
    Without decoding it, a ``verdict`` the model wrote is a few characters
    inside a string, and nothing downstream can gate on it. Only a whole JSON
    *object* is decoded: an array, a number or prose stays text, because the
    step's output is an object and guessing at a wrapper would invent a shape
    the API never sent.
    """
    candidate = text.strip()

    if not candidate.startswith("{"):
        return text

    try:
        decoded = json.loads(candidate)
    except json.JSONDecodeError:
        return text

    return decoded if isinstance(decoded, dict) else text


def _clip(text: str) -> str:
    """Return text bounded to a size worth storing and passing downstream.

    Args:
        text: Captured output.

    Returns:
        The text, truncated with a marker when it is too long.
    """
    if len(text) <= _MAX_CAPTURED_CHARS:
        return text

    return f"{text[:_MAX_CAPTURED_CHARS]}\n... [{len(text)} chars truncated]"


class HttpToolTransport:
    """Calls an ordinary REST API and reads its answer as a step result."""

    def close(self) -> None:
        """Do nothing: each request opens and closes its own connection."""

    def send(
        self,
        message: ExecutionMessage,
        agent: AgentEndpoint,
        timeout: float,
    ) -> dict[str, Any]:
        """Call the configured URL and turn the response into a reply.

        Args:
            message: The execution message to deliver.
            agent: The resolved registry entry, carrying an `HttpToolSpec`.
            timeout: Seconds to wait for the response.

        Returns:
            The reply in the KIO1-KIO10 shape.

        Raises:
            AgentTransportError: If the entry is not an HTTP tool, a needed
                environment variable is unset, or the call failed.
        """
        # Imported here rather than at module scope so this module stays
        # importable in environments that stub urllib for tests.
        import urllib.error
        import urllib.request

        spec = agent.tool

        if not isinstance(spec, HttpToolSpec):
            raise AgentTransportError(
                f"{agent.agent_id} is not configured as an HTTP tool."
            )

        message = self._with_arguments(message, spec)
        url = render_url(spec.url, message)
        headers = {
            name: expand_environment(
                str(render(value, message)),
                agent_id=agent.agent_id,
                field=f"header {name}",
            )
            for name, value in spec.headers.items()
        }

        body: bytes | None = None

        if spec.method in {"POST", "PUT", "PATCH"}:
            # An API that already speaks the protocol needs no `body` at all,
            # so the unconfigured default is the execution message itself.
            payload = (
                message.to_payload()
                if spec.body is None
                else render(spec.body, message)
            )
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")

        inject(headers)

        request = urllib.request.Request(
            str(url),
            data=body,
            headers=headers,
            method=spec.method,
        )

        logger.debug(
            "Calling HTTP tool: step_id=%s agent_id=%s method=%s url=%s",
            message.step_id,
            agent.agent_id,
            spec.method,
            url,
        )

        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                text = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace").strip()
            raise AgentTransportError(
                f"{agent.agent_id} returned HTTP {error.code}: "
                f"{_clip(' '.join(detail.split())) or '<empty body>'}"
            ) from error
        except urllib.error.URLError as error:
            raise AgentTransportError(
                f"{agent.agent_id} is unreachable at {url}: {error.reason}"
            ) from error
        except TimeoutError as error:
            raise AgentTransportError(
                f"{agent.agent_id} did not answer within {timeout:g}s"
            ) from error

        return protocol_reply(message, status="ok", output=self._output(text, spec))

    @staticmethod
    def _with_arguments(
        message: ExecutionMessage, spec: HttpToolSpec
    ) -> ExecutionMessage:
        """Settle the arguments an API declares before anything is rendered.

        The rules are the MCP ones, for the same reasons: arguments the plan
        supplied are used, a lone string argument takes the task text, and a
        required argument nothing supplies fails the step by name — calling
        the API with `{arguments.latitude}` left in its URL would only produce
        an error from the far side that says less.

        Args:
            message: The message being dispatched.
            spec: The API's configuration.

        Returns:
            The message, with the settled arguments in `data` when the API
            declares any.

        Raises:
            AgentTransportError: If a required argument is missing.
        """
        tool = spec.arguments_tool(message.capability)

        if tool is None:
            return message

        # Imported here: the MCP transport imports this module for `render`.
        from dispatch.mcp_transport import ARGUMENTS_KEY, arguments_for

        arguments = arguments_for(message, tool)
        missing = [name for name in tool.required if name not in arguments]

        if missing:
            raise AgentTransportError(
                f"{message.agent_id} needs argument(s) {', '.join(missing)} "
                f"({tool.summarize_arguments()}), and the step did not supply "
                "them."
            )

        return replace(message, data={**message.data, ARGUMENTS_KEY: arguments})

    @staticmethod
    def _output(text: str, spec: HttpToolSpec) -> dict[str, Any]:
        """Turn a response body into the step's output object.

        Args:
            text: The raw response body.
            spec: The tool's configuration, whose `result_path` selects part
                of a decoded response.

        Returns:
            An output object. A response that is not a JSON object is kept
            under `result`, so a plain-text API is still usable downstream.
        """
        try:
            decoded: Any = json.loads(text)
        except json.JSONDecodeError:
            return {"result": _clip(text)}

        for key in spec.result_path:
            if not isinstance(decoded, dict) or key not in decoded:
                return {
                    "result": decoded,
                    "warning": (
                        f"result_path key {key!r} is not in the response; "
                        "the whole response is kept instead."
                    ),
                }
            decoded = decoded[key]

        if spec.result_path and isinstance(decoded, str):
            decoded = _object_in_text(decoded)

        if isinstance(decoded, dict):
            return decoded

        return {"result": decoded}


class CliToolTransport:
    """Runs a local command and reads its exit code as a step result."""

    def close(self) -> None:
        """Do nothing: each step runs its own child process to completion."""

    def send(
        self,
        message: ExecutionMessage,
        agent: AgentEndpoint,
        timeout: float,
    ) -> dict[str, Any]:
        """Run the configured command and turn its result into a reply.

        Args:
            message: The execution message to deliver.
            agent: The resolved registry entry, carrying a `CliToolSpec`.
            timeout: Seconds to allow the command before killing it.

        Returns:
            The reply in the KIO1-KIO10 shape. A non-zero exit is reported as
            a failed step, not raised, because a tool that ran and said "no"
            is a result rather than a broken transport.

        Raises:
            AgentTransportError: If the entry is not a CLI tool, the program
                is missing, or it had to be killed on timeout.
        """
        spec = agent.tool

        if not isinstance(spec, CliToolSpec):
            raise AgentTransportError(
                f"{agent.agent_id} is not configured as a CLI tool."
            )

        # Every argument is rendered on its own and passed as its own element.
        # Nothing is ever joined into a string for a shell to parse, so a task
        # description containing quotes or semicolons stays one argument.
        command = [str(render(part, message)) for part in spec.command]

        environment = dict(os.environ)
        environment.update(
            {
                name: expand_environment(
                    str(render(value, message)),
                    agent_id=agent.agent_id,
                    field=f"env {name}",
                )
                for name, value in spec.env.items()
            }
        )

        logger.debug(
            "Running CLI tool: step_id=%s agent_id=%s command=%s",
            message.step_id,
            agent.agent_id,
            command,
        )

        try:
            # An argument list with shell=False: nothing here reaches a shell.
            completed = subprocess.run(
                command,
                cwd=spec.cwd or None,
                env=environment,
                capture_output=True,
                timeout=timeout,
                shell=False,
                check=False,
            )
        except FileNotFoundError as error:
            raise AgentTransportError(
                f"{agent.agent_id} could not run {command[0]!r}: not found on "
                "this machine."
            ) from error
        except NotADirectoryError as error:
            raise AgentTransportError(
                f"{agent.agent_id} could not run in {spec.cwd!r}: not a directory."
            ) from error
        except subprocess.TimeoutExpired as error:
            raise AgentTransportError(
                f"{agent.agent_id} did not finish within {timeout:g}s and was "
                "stopped."
            ) from error
        except OSError as error:
            raise AgentTransportError(
                f"{agent.agent_id} could not run {command[0]!r}: {error}"
            ) from error

        stdout = decode_output(completed.stdout)
        stderr = decode_output(completed.stderr)

        if completed.returncode != 0:
            logger.info(
                "CLI tool reported failure: step_id=%s agent_id=%s exit_code=%d",
                message.step_id,
                agent.agent_id,
                completed.returncode,
            )
            reason = _clip((stderr or stdout).strip()) or "no output"
            return protocol_reply(
                message,
                status="error",
                error=f"{agent.agent_id} exited {completed.returncode}: {reason}",
            )

        return protocol_reply(message, status="ok", output=self._output(stdout, stderr))

    @staticmethod
    def _output(stdout: str, stderr: str) -> dict[str, Any]:
        """Turn a successful command's streams into the step's output object.

        Args:
            stdout: What the command wrote to standard output.
            stderr: What it wrote to standard error.

        Returns:
            The parsed object when the command printed one, which lets a tool
            report `verdict` and `hitl_required` the same way a KIO does.
            Otherwise the captured streams.
        """
        try:
            decoded: Any = json.loads(stdout)
        except json.JSONDecodeError:
            decoded = None

        if isinstance(decoded, dict):
            return decoded

        return {
            "stdout": _clip(stdout.strip()),
            "stderr": _clip(stderr.strip()),
            "exit_code": 0,
        }
