import json
import logging
import urllib.error
import urllib.request
from typing import Any, Protocol

from opentelemetry.propagate import inject

from dispatch.agent_registry import AgentEndpoint
from dispatch.workflow_planner import ExecutionMessage

logger = logging.getLogger(__name__)

_MAX_ERROR_BODY_CHARS = 512


class AgentTransportError(Exception):
    """Raised when an execution message could not be delivered or answered."""


class AgentTransport(Protocol):
    """How KIO1 delivers one execution message to one agent.

    The shared documents disagree on the transport: the observability guide
    describes NATS JetStream carrying a `KIOEnvelope`, while the KIO1-KIO10
    integration protocol describes a direct HTTP call to an `endpoint`. Both
    fit behind this interface, so the choice can be made later without
    touching the planner or the executor.
    """

    def send(
        self,
        message: ExecutionMessage,
        agent: AgentEndpoint,
        timeout: float,
    ) -> dict[str, Any]:
        """Deliver an execution message and return the agent's decoded reply.

        Args:
            message: The execution message to deliver.
            agent: The resolved endpoint the message is addressed to.
            timeout: Seconds to wait for the agent's reply.

        Returns:
            The agent's reply as a decoded JSON object.

        Raises:
            AgentTransportError: If delivery, the reply, or decoding failed.
        """
        ...


class HttpAgentTransport:
    """Delivers execution messages over HTTP, per the KIO1-KIO10 protocol.

    Uses `urllib.request` rather than a third-party HTTP client, matching the
    existing Ollama client and keeping the dispatch layer dependency-free.
    """

    def send(
        self,
        message: ExecutionMessage,
        agent: AgentEndpoint,
        timeout: float,
    ) -> dict[str, Any]:
        """Post an execution message to the agent and decode its reply.

        Args:
            message: The execution message to deliver.
            agent: The resolved endpoint the message is addressed to.
            timeout: Seconds to wait for the agent's reply.

        Returns:
            The agent's reply as a decoded JSON object.

        Raises:
            AgentTransportError: If the request failed, the agent answered
                with a non-2xx status, or the reply was not a JSON object.
        """
        payload = json.dumps(message.to_payload()).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        # W3C trace context, so an agent that reads `traceparent` continues
        # this trace instead of starting an unrelated one. Agents that ignore
        # the header are unaffected.
        inject(headers)

        request = urllib.request.Request(
            agent.url,
            data=payload,
            headers=headers,
            method="POST",
        )

        logger.debug(
            "Dispatching step: workflow_id=%s step_id=%s url=%s",
            message.workflow_id,
            message.step_id,
            agent.url,
        )

        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            detail = _read_error_body(error)
            raise AgentTransportError(
                f"{message.agent_id} returned HTTP {error.code}: {detail}"
            ) from error
        except urllib.error.URLError as error:
            raise AgentTransportError(
                f"{message.agent_id} is unreachable at {agent.url}: {error.reason}"
            ) from error
        except TimeoutError as error:
            raise AgentTransportError(
                f"{message.agent_id} did not answer within {timeout:g}s"
            ) from error

        try:
            decoded: Any = json.loads(body)
        except json.JSONDecodeError as error:
            raise AgentTransportError(
                f"{message.agent_id} returned a non-JSON reply: {error}"
            ) from error

        if not isinstance(decoded, dict):
            raise AgentTransportError(
                f"{message.agent_id} returned {type(decoded).__name__}, "
                "expected a JSON object"
            )

        return decoded


def _read_error_body(error: urllib.error.HTTPError) -> str:
    """Return a bounded, single-line description of an HTTP error body.

    Args:
        error: The raised HTTP error.

    Returns:
        The truncated response body, or a placeholder when it is unreadable.
    """
    try:
        body = error.read().decode("utf-8", errors="replace").strip()
    except (OSError, ValueError):
        return "<unreadable body>"

    if not body:
        return "<empty body>"

    collapsed = " ".join(body.split())

    if len(collapsed) > _MAX_ERROR_BODY_CHARS:
        return f"{collapsed[:_MAX_ERROR_BODY_CHARS]}..."

    return collapsed
