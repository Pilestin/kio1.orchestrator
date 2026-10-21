"""A client for Model Context Protocol servers.

MCP is the reason this module exists at all. Every other registry kind makes
the person adding a target describe it: what it is called, what it accepts,
what comes back. That description is written by hand, in `config.json`, and
it is wrong the moment the target changes. An MCP server carries its own
description — it will tell you its tools, what each one does, and the exact
shape of each one's arguments — so the entry shrinks to *how to start it*,
and everything else is asked for rather than declared.

What is implemented here is the client half of that conversation:

- **stdio**, where the server is a child process and messages are
  newline-delimited JSON on its pipes. This is what almost every published
  server uses (`npx -y @modelcontextprotocol/server-filesystem …`).
- **Streamable HTTP**, where the server is a URL that answers JSON-RPC over
  POST. This is how a remote or shared server is reached.

Three methods carry the whole feature: `initialize` opens a session,
`tools/list` is the self-description, `tools/call` runs one.

Why this is written rather than taken from the official SDK: that SDK is
async, and KIO1's executor is threads and blocking calls all the way down.
Bridging the two would mean either an event loop per call or converting the
executor, and both cost more than the ~200 lines the wire protocol actually
needs. The value being adopted is the *protocol* and its ecosystem of
servers, which this speaks exactly; the boundary is `McpSession`, so an SDK
can replace the inside of this file without anything else noticing.
"""

import json
import logging
import os
import queue
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# The revision of MCP this client implements. Servers report their own in the
# initialize result; a server speaking an older one still works, because
# nothing used here changed between revisions.
PROTOCOL_VERSION = "2025-06-18"

CLIENT_NAME = "kio1-orchestrator"
CLIENT_VERSION = "1.0"

# Enough of a chatty server's standard error to explain a failure, without
# letting a server that logs a line per millisecond grow without bound.
_STDERR_LINES = 40

# A tool result is fed into the next step's prompt, so it is bounded for the
# same reason every other transport bounds its output.
_MAX_RESULT_CHARS = 8000


class McpError(Exception):
    """Raised when an MCP server could not be reached or refused a call."""


@dataclass(frozen=True)
class McpTool:
    """One tool an MCP server says it has.

    Attributes:
        name: What `tools/call` is asked for, and what a plan step names as
            its capability.
        description: What the server says it does, shown to the planner.
        input_schema: JSON Schema for the tool's arguments. This is the part
            no hand-written registry entry could have supplied, and it is what
            lets the planner send arguments rather than prose.
    """

    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)

    @property
    def required(self) -> tuple[str, ...]:
        """Return the argument names the server insists on."""
        required = self.input_schema.get("required")

        if not isinstance(required, list):
            return ()

        return tuple(name for name in required if isinstance(name, str))

    @property
    def properties(self) -> dict[str, Any]:
        """Return the declared arguments, keyed by name."""
        properties = self.input_schema.get("properties")

        return properties if isinstance(properties, dict) else {}

    def sole_string_argument(self) -> str | None:
        """Return the one string argument this tool needs, when there is one.

        A great many MCP tools take a single string — a query, a path, a
        command. For those, a plan step's free-text task is already the
        argument, and asking the planner to wrap it in an object would be
        ceremony. Anything more complicated gets no such guess.

        Returns:
            The argument name, or None when the tool does not have exactly one
            required string argument.
        """
        required = self.required

        if len(required) != 1:
            return None

        name = required[0]
        declared = self.properties.get(name)

        if not isinstance(declared, dict) or declared.get("type") != "string":
            return None

        return name

    def summarize_arguments(self) -> str:
        """Return the tool's arguments as one line for the planner's catalog.

        Returns:
            A rendering such as `path (string, required), depth (integer)`, or
            an empty string for a tool that takes none.
        """
        properties = self.properties

        if not properties:
            return ""

        required = set(self.required)
        parts: list[str] = []

        for name, declared in properties.items():
            kind = ""

            if isinstance(declared, dict):
                declared_type = declared.get("type")
                if isinstance(declared_type, str):
                    kind = declared_type

            annotation = kind
            if name in required:
                annotation = f"{kind}, required" if kind else "required"

            parts.append(f"{name} ({annotation})" if annotation else name)

        return ", ".join(parts)


def _decode_tools(payload: Any) -> tuple[McpTool, ...]:
    """Turn a `tools/list` result into tools, skipping anything malformed.

    Args:
        payload: The decoded `result` object.

    Returns:
        Every usable tool. A single unreadable entry is dropped rather than
        failing the listing: one bad tool must not make the other twenty
        unreachable.
    """
    listed = payload.get("tools") if isinstance(payload, dict) else None

    if not isinstance(listed, list):
        return ()

    tools: list[McpTool] = []

    for entry in listed:
        if not isinstance(entry, dict):
            continue

        name = entry.get("name")

        if not isinstance(name, str) or not name.strip():
            continue

        # `inputSchema` is the spec's spelling; some servers emit the
        # snake_case form, and accepting both costs one line.
        schema = entry.get("inputSchema")
        if not isinstance(schema, dict):
            schema = entry.get("input_schema")

        description = entry.get("description")

        tools.append(
            McpTool(
                name=name.strip(),
                description=(
                    description.strip() if isinstance(description, str) else ""
                ),
                input_schema=schema if isinstance(schema, dict) else {},
            )
        )

    return tuple(tools)


def flatten_content(result: dict[str, Any]) -> dict[str, Any]:
    """Turn a `tools/call` result into the output object a step records.

    MCP answers in content blocks — text, images, embedded resources — plus,
    since the 2025-06-18 revision, an optional `structuredContent` holding the
    same answer as data. The structured form is preferred when the server sent
    one, because the next step in a workflow reads fields rather than prose.

    Args:
        result: The decoded `result` object of a `tools/call`.

    Returns:
        An output object. Text blocks are joined under `result`; anything that
        is not text is summarized by type rather than dropped silently, so an
        image coming back is visible instead of looking like an empty answer.
    """
    structured = result.get("structuredContent")

    if isinstance(structured, dict) and structured:
        return structured

    blocks = result.get("content")

    if not isinstance(blocks, list):
        return {"result": ""}

    texts: list[str] = []
    others: list[str] = []

    for block in blocks:
        if not isinstance(block, dict):
            continue

        if block.get("type") == "text" and isinstance(block.get("text"), str):
            texts.append(block["text"])
            continue

        kind = block.get("type")
        others.append(str(kind) if isinstance(kind, str) else "unknown")

    joined = "\n".join(texts).strip()

    if len(joined) > _MAX_RESULT_CHARS:
        joined = (
            f"{joined[:_MAX_RESULT_CHARS]}\n" f"... [{len(joined)} chars truncated]"
        )

    output: dict[str, Any] = {"result": joined}

    if others:
        output["attachments"] = others

    return output


class McpSession:
    """One open conversation with an MCP server.

    A session is deliberately long-lived. Starting a server is the expensive
    part — `npx` fetching a package takes seconds — while a call is cheap, so
    the process is opened once and reused for every step that addresses it.

    Calls are serialized by a lock. A stdio server has one pair of pipes, and
    KIO1 runs the steps of a parallel level at the same time; without the lock
    two steps would interleave their frames on the same pipe. The cost is that
    two concurrent steps on *one* server wait for each other, which is the
    correct trade: they were always sharing one process.
    """

    def __init__(self, agent_id: str, timeout: float) -> None:
        """Prepare a session without opening it.

        Args:
            agent_id: The registry identifier, used in every error message.
            timeout: Seconds to wait for a single response.
        """
        self._agent_id = agent_id
        self._timeout = timeout
        self._lock = threading.Lock()
        self._next_id = 0
        self._opened = False

    # ── what a subclass provides ────────────────────────────────────────────

    def _open(self) -> None:
        """Establish the underlying connection."""
        raise NotImplementedError

    def _exchange(self, request: dict[str, Any], timeout: float) -> dict[str, Any]:
        """Send one JSON-RPC request and return the decoded reply."""
        raise NotImplementedError

    def _notify(self, notification: dict[str, Any]) -> None:
        """Send one JSON-RPC notification, which has no reply."""
        raise NotImplementedError

    def close(self) -> None:
        """Release the underlying connection."""
        raise NotImplementedError

    @property
    def address(self) -> str:
        """Return what this session connects to, for error messages."""
        raise NotImplementedError

    # ── the protocol ────────────────────────────────────────────────────────

    def _identifier(self) -> int:
        """Return the next JSON-RPC request id."""
        self._next_id += 1

        return self._next_id

    def _call(self, method: str, params: dict[str, Any], timeout: float) -> Any:
        """Make one JSON-RPC call and return its result.

        Args:
            method: The method name.
            params: The method's parameters.
            timeout: Seconds to wait.

        Returns:
            The decoded `result`.

        Raises:
            McpError: If the transport failed or the server returned an error.
        """
        reply = self._exchange(
            {
                "jsonrpc": "2.0",
                "id": self._identifier(),
                "method": method,
                "params": params,
            },
            timeout,
        )

        if "error" in reply:
            error = reply["error"]
            message = error.get("message") if isinstance(error, dict) else None
            raise McpError(
                f"{self._agent_id} refused {method}: "
                f"{message or json.dumps(error, ensure_ascii=False)}"
            )

        result = reply.get("result")

        return result if result is not None else {}

    def ensure_open(self) -> None:
        """Open and hand-shake the session if it is not already running.

        Raises:
            McpError: If the server could not be started or would not
                complete the handshake.
        """
        with self._lock:
            self._ensure_open_locked()

    def _ensure_open_locked(self) -> None:
        """Open the session, assuming the lock is already held."""
        if self._opened:
            return

        self._open()

        # The handshake is not optional: a server is entitled to refuse every
        # other method until it has happened.
        self._call(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            },
            self._timeout,
        )
        self._notify({"jsonrpc": "2.0", "method": "notifications/initialized"})

        self._opened = True

        logger.info(
            "MCP session opened: agent_id=%s address=%s",
            self._agent_id,
            self.address,
        )

    def list_tools(self) -> tuple[McpTool, ...]:
        """Ask the server what it can do.

        Returns:
            Every tool the server declares, across as many pages as it uses.

        Raises:
            McpError: If the server could not be reached or listed.
        """
        with self._lock:
            self._ensure_open_locked()

            tools: list[McpTool] = []
            cursor: str | None = None

            # A server with many tools pages them. Following the cursor
            # matters: a half-read listing looks like a server that simply
            # does not have the tool a plan asked for.
            while True:
                params: dict[str, Any] = {} if cursor is None else {"cursor": cursor}
                result = self._call("tools/list", params, self._timeout)
                tools.extend(_decode_tools(result))

                following = (
                    result.get("nextCursor") if isinstance(result, dict) else None
                )

                if not isinstance(following, str) or not following:
                    return tuple(tools)

                cursor = following

    def call_tool(
        self, name: str, arguments: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        """Run one tool and return its raw result.

        Args:
            name: The tool to run.
            arguments: Its arguments, already matched to the tool's schema.
            timeout: Seconds to wait — a tool call is real work and is given
                the step's own timeout rather than the handshake's.

        Returns:
            The decoded `result` object, including `isError` when the server
            reported the tool itself failing.

        Raises:
            McpError: If the server could not be reached or refused the call.
        """
        with self._lock:
            self._ensure_open_locked()

            result = self._call(
                "tools/call", {"name": name, "arguments": arguments}, timeout
            )

        return result if isinstance(result, dict) else {"result": result}


class StdioMcpSession(McpSession):
    """An MCP server running as a child process.

    Messages are newline-delimited JSON on the child's standard input and
    output. Two background threads do the reading: one turns stdout into
    parsed frames, the other drains stderr. Draining stderr is not optional —
    a server that logs to it (most of them, chattily) would otherwise fill the
    pipe buffer and deadlock waiting for someone to read.
    """

    def __init__(
        self,
        agent_id: str,
        command: tuple[str, ...],
        *,
        cwd: str = "",
        env: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> None:
        """Prepare to run a server without starting it.

        Args:
            agent_id: The registry identifier.
            command: Program and arguments, run without a shell.
            cwd: Working directory, empty for KIO1's own.
            env: Extra environment variables for the child.
            timeout: Seconds to wait for a single response.
        """
        super().__init__(agent_id, timeout)
        self._command = command
        self._cwd = cwd
        self._env = dict(env or {})
        self._process: subprocess.Popen[str] | None = None
        self._frames: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._stderr: deque[str] = deque(maxlen=_STDERR_LINES)

    @property
    def address(self) -> str:
        """Return the program being run."""
        return " ".join(self._command)

    def _open(self) -> None:
        """Start the child process and its reader threads.

        Raises:
            McpError: If the program is missing or could not be started.
        """
        environment = dict(os.environ)
        environment.update(self._env)

        try:
            # An argument list with shell=False, like every other command KIO1
            # runs: nothing in a task description reaches a shell.
            self._process = subprocess.Popen(
                list(self._command),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self._cwd or None,
                env=environment,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                shell=False,
            )
        except FileNotFoundError as error:
            raise McpError(
                f"{self._agent_id} could not start {self._command[0]!r}: not "
                "found on this machine."
            ) from error
        except NotADirectoryError as error:
            raise McpError(
                f"{self._agent_id} could not run in {self._cwd!r}: not a directory."
            ) from error
        except OSError as error:
            raise McpError(
                f"{self._agent_id} could not start {self._command[0]!r}: {error}"
            ) from error

        threading.Thread(
            target=self._read_frames,
            name=f"kio1-mcp-{self._agent_id}-out",
            daemon=True,
        ).start()
        threading.Thread(
            target=self._read_stderr,
            name=f"kio1-mcp-{self._agent_id}-err",
            daemon=True,
        ).start()

    def _read_frames(self) -> None:
        """Parse the child's stdout into frames until it closes."""
        process = self._process

        if process is None or process.stdout is None:  # pragma: no cover
            return

        for line in process.stdout:
            line = line.strip()

            if not line:
                continue

            try:
                decoded = json.loads(line)
            except json.JSONDecodeError:
                # Servers that print a banner before speaking the protocol are
                # common enough that one unparsable line is not a failure.
                logger.debug(
                    "Ignoring non-JSON line from MCP server %s: %s",
                    self._agent_id,
                    line[:200],
                )
                continue

            if isinstance(decoded, dict):
                self._frames.put(decoded)

        # Unblocks anyone waiting on a reply from a server that has exited.
        self._frames.put(None)

    def _read_stderr(self) -> None:
        """Keep the tail of the child's standard error."""
        process = self._process

        if process is None or process.stderr is None:  # pragma: no cover
            return

        for line in process.stderr:
            stripped = line.rstrip()

            if stripped:
                self._stderr.append(stripped)

    def _diagnosis(self) -> str:
        """Return why the server is not answering, in as much detail as there is."""
        process = self._process

        if process is not None and process.poll() is not None:
            tail = " | ".join(list(self._stderr)[-5:])
            reason = f"exited with code {process.returncode}"

            return f"{reason}: {tail}" if tail else reason

        tail = " | ".join(list(self._stderr)[-3:])

        return tail or "no reply"

    def _send(self, message: dict[str, Any]) -> None:
        """Write one newline-delimited frame to the child.

        Args:
            message: The JSON-RPC message.

        Raises:
            McpError: If the child's input could not be written to.
        """
        process = self._process

        if process is None or process.stdin is None:  # pragma: no cover
            raise McpError(f"{self._agent_id} has no open MCP session.")

        # The transport forbids embedded newlines, which is exactly what
        # separates one frame from the next.
        rendered = json.dumps(message, ensure_ascii=False).replace("\n", " ")

        try:
            process.stdin.write(rendered + "\n")
            process.stdin.flush()
        except (BrokenPipeError, ValueError, OSError) as error:
            raise McpError(
                f"{self._agent_id} stopped listening: {self._diagnosis()}"
            ) from error

    def _exchange(self, request: dict[str, Any], timeout: float) -> dict[str, Any]:
        """Send a request and wait for the reply carrying its id.

        Args:
            request: The JSON-RPC request.
            timeout: Seconds to wait.

        Returns:
            The matching reply.

        Raises:
            McpError: If the server exited, or did not answer in time.
        """
        self._send(request)

        deadline = time.monotonic() + timeout

        while True:
            remaining = deadline - time.monotonic()

            if remaining <= 0:
                raise McpError(
                    f"{self._agent_id} did not answer {request['method']} "
                    f"within {timeout:g}s."
                )

            try:
                frame = self._frames.get(timeout=remaining)
            except queue.Empty:
                raise McpError(
                    f"{self._agent_id} did not answer {request['method']} "
                    f"within {timeout:g}s."
                ) from None

            if frame is None:
                raise McpError(f"{self._agent_id} {self._diagnosis()}")

            # Servers send notifications and their own requests down the same
            # pipe. Anything that is not the reply being waited for is not an
            # error; it is simply not this.
            if frame.get("id") == request["id"]:
                return frame

            logger.debug(
                "Ignoring unsolicited MCP frame from %s: method=%s id=%s",
                self._agent_id,
                frame.get("method"),
                frame.get("id"),
            )

    def _notify(self, notification: dict[str, Any]) -> None:
        """Send a notification, which the server does not answer."""
        self._send(notification)

    def close(self) -> None:
        """Stop the child process, politely and then not."""
        process = self._process
        self._process = None
        self._opened = False

        if process is None:
            return

        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:  # pragma: no cover - already-broken pipe
            pass

        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.info(
                "MCP server %s did not exit on its own; stopping it",
                self._agent_id,
            )
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover
                logger.warning("MCP server %s could not be stopped", self._agent_id)


class HttpMcpSession(McpSession):
    """An MCP server reached over Streamable HTTP.

    Each JSON-RPC message is a POST. The server may answer with a single JSON
    object or with an event stream carrying one; both are read here, because
    which you get is the server's choice rather than the client's.
    """

    def __init__(
        self,
        agent_id: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> None:
        """Prepare to talk to a server without contacting it.

        Args:
            agent_id: The registry identifier.
            url: The server's endpoint.
            headers: Extra headers, typically an Authorization line.
            timeout: Seconds to wait for a single response.
        """
        super().__init__(agent_id, timeout)
        self._url = url
        self._headers = dict(headers or {})
        self._session_id = ""

    @property
    def address(self) -> str:
        """Return the URL being called."""
        return self._url

    def _open(self) -> None:
        """Do nothing: an HTTP session is established by the first POST."""

    def _request_headers(self) -> dict[str, str]:
        """Return the headers every message carries."""
        headers = dict(self._headers)
        headers.setdefault("Content-Type", "application/json")
        # Declaring both is what makes the server free to answer either way.
        headers.setdefault("Accept", "application/json, text/event-stream")

        if self._opened:
            headers.setdefault("MCP-Protocol-Version", PROTOCOL_VERSION)

        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        return headers

    def _post(self, message: dict[str, Any], timeout: float) -> tuple[str, Any]:
        """POST one message and return its body and session header.

        Args:
            message: The JSON-RPC message.
            timeout: Seconds to wait.

        Returns:
            A pair of (body, session id header or empty).

        Raises:
            McpError: If the server could not be reached or refused the POST.
        """
        import urllib.error
        import urllib.request

        request = urllib.request.Request(
            self._url,
            data=json.dumps(message, ensure_ascii=False).encode("utf-8"),
            headers=self._request_headers(),
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return (
                    response.read().decode("utf-8", errors="replace"),
                    response.headers.get("Mcp-Session-Id", "") or "",
                )
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace").strip()
            raise McpError(
                f"{self._agent_id} answered HTTP {error.code} at {self._url}: "
                f"{' '.join(detail.split())[:400] or '<empty body>'}"
            ) from error
        except urllib.error.URLError as error:
            raise McpError(
                f"{self._agent_id} is unreachable at {self._url}: {error.reason}"
            ) from error
        except TimeoutError as error:
            raise McpError(
                f"{self._agent_id} did not answer within {timeout:g}s."
            ) from error

    @staticmethod
    def _decode(body: str, identifier: Any) -> dict[str, Any] | None:
        """Find the reply to one request in a response body.

        Args:
            body: The response body, either a JSON object or an event stream.
            identifier: The request id being answered.

        Returns:
            The matching reply, or None when the body holds none.
        """
        candidates: list[str] = []

        stripped = body.strip()

        if stripped.startswith(("{", "[")):
            candidates.append(stripped)
        else:
            # Server-sent events: the payloads are the `data:` lines, and the
            # stream may carry notifications before the reply.
            candidates.extend(
                line[len("data:") :].strip()
                for line in body.splitlines()
                if line.startswith("data:")
            )

        for candidate in candidates:
            try:
                decoded = json.loads(candidate)
            except json.JSONDecodeError:
                continue

            frames = decoded if isinstance(decoded, list) else [decoded]

            for frame in frames:
                if isinstance(frame, dict) and frame.get("id") == identifier:
                    return frame

        return None

    def _exchange(self, request: dict[str, Any], timeout: float) -> dict[str, Any]:
        """POST a request and return the reply carrying its id.

        Args:
            request: The JSON-RPC request.
            timeout: Seconds to wait.

        Returns:
            The matching reply.

        Raises:
            McpError: If the server answered with nothing usable.
        """
        body, session_id = self._post(request, timeout)

        if session_id:
            # Issued on initialize and echoed from then on; a server that
            # keeps state needs it back or every later call looks new.
            self._session_id = session_id

        reply = self._decode(body, request["id"])

        if reply is None:
            raise McpError(
                f"{self._agent_id} answered {request['method']} with nothing "
                f"usable: {' '.join(body.split())[:400] or '<empty body>'}"
            )

        return reply

    def _notify(self, notification: dict[str, Any]) -> None:
        """POST a notification, tolerating a server that does not want it.

        A notification has no reply, so a server answering 202 with an empty
        body is the normal case and a rejection tells us nothing actionable —
        the next real call will fail with a reason worth reporting.
        """
        try:
            self._post(notification, self._timeout)
        except McpError:
            logger.debug(
                "MCP server %s did not accept %s",
                self._agent_id,
                notification.get("method"),
            )

    def close(self) -> None:
        """Forget the session id; HTTP holds nothing else open."""
        self._session_id = ""
        self._opened = False


class McpSessionPool:
    """Keeps one open session per MCP target, and hands it out.

    Starting a server is slow and calling it is fast, so sessions are kept
    rather than made per call. A session that has died is discarded and
    reopened on the next use: an MCP server is a child process, and one that
    crashed must not make its target permanently unusable.
    """

    def __init__(self, timeout: float = 30.0) -> None:
        """Create an empty pool.

        Args:
            timeout: Seconds a handshake or listing may take.
        """
        self._timeout = timeout
        self._sessions: dict[str, McpSession] = {}
        self._lock = threading.Lock()

    def session_for(self, agent_id: str, factory: Any) -> McpSession:
        """Return the open session for a target, opening one if needed.

        Args:
            agent_id: The registry identifier.
            factory: Called with no arguments to build a session when none is
                open. Taking a factory rather than a spec keeps the pool from
                having to know how any particular kind of target is addressed.

        Returns:
            An opened, hand-shaken session.

        Raises:
            McpError: If the session could not be opened.
        """
        with self._lock:
            session = self._sessions.get(agent_id)

            if session is None:
                session = factory()
                self._sessions[agent_id] = session

        try:
            session.ensure_open()
        except McpError:
            self.discard(agent_id)
            raise

        return session

    def discard(self, agent_id: str) -> None:
        """Close and forget one target's session.

        Args:
            agent_id: The registry identifier.
        """
        with self._lock:
            session = self._sessions.pop(agent_id, None)

        if session is None:
            return

        try:
            session.close()
        except Exception:  # pragma: no cover - closing must not raise
            logger.warning(
                "MCP session for %s failed to close", agent_id, exc_info=True
            )

    def close(self) -> None:
        """Close every session, so one failure cannot leak the others."""
        for agent_id in list(self._sessions):
            self.discard(agent_id)
