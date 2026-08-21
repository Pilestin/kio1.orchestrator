import asyncio
import json
import logging
import threading
from concurrent.futures import Future
from typing import Any

from opentelemetry.propagate import inject

from dispatch.agent_registry import AgentEndpoint
from dispatch.agent_transport import AgentTransportError
from dispatch.workflow_planner import ExecutionMessage

logger = logging.getLogger(__name__)

DEFAULT_SUBJECT_TEMPLATE = "kio.{agent}.execute"

# Margin added to the caller's timeout when waiting on the event-loop thread,
# so the NATS request times out first and produces a clear error rather than
# the bridge giving up on it.
_BRIDGE_TIMEOUT_MARGIN_SECONDS = 5.0


class NatsAgentTransport:
    """Delivers execution messages over NATS request/reply.

    The message body is byte-identical to the one the HTTP transport posts:
    the KIO1-KIO10 execution message, unchanged. Only the way it travels
    differs. Routing and correlation metadata that has no place in that
    schema rides in NATS headers instead.

    nats-py is asyncio-only while the executor is synchronous and
    thread-based, so a single event loop is run on a dedicated daemon thread
    and requests are handed to it. One connection is shared by every
    dispatch, including the concurrent ones in `parallel` mode.
    """

    def __init__(
        self,
        servers: list[str],
        *,
        subject_template: str = DEFAULT_SUBJECT_TEMPLATE,
        connect_timeout: float = 10.0,
    ) -> None:
        """Store connection settings without connecting yet.

        Args:
            servers: NATS server URLs, such as `["nats://localhost:4222"]`.
            subject_template: Used for agents with no explicit subject.
                `{agent}` is replaced with the lowercased agent id.
            connect_timeout: Seconds to wait for the initial connection.
        """
        self._servers = list(servers)
        self._subject_template = subject_template
        self._connect_timeout = connect_timeout
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._connection: Any = None
        self._lock = threading.Lock()

    def subject_for(self, agent: AgentEndpoint) -> str:
        """Return the subject an agent's messages are published to.

        Args:
            agent: The resolved agent.

        Returns:
            The agent's configured subject, or one derived from the template.
        """
        if agent.subject:
            return agent.subject

        return self._subject_template.format(agent=agent.agent_id.lower())

    def send(
        self,
        message: ExecutionMessage,
        agent: AgentEndpoint,
        timeout: float,
    ) -> dict[str, Any]:
        """Publish an execution message and wait for the agent's reply.

        Args:
            message: The execution message to deliver.
            agent: The resolved endpoint the message is addressed to.
            timeout: Seconds to wait for the agent's reply.

        Returns:
            The agent's reply as a decoded JSON object.

        Raises:
            AgentTransportError: If publishing, the reply, or decoding failed.
        """
        subject = self.subject_for(agent)
        payload = json.dumps(message.to_payload()).encode("utf-8")

        headers = {
            "kio_id": message.agent_id,
            "workflow_id": message.workflow_id,
            "step_id": message.step_id,
        }
        # W3C trace context, so an agent that reads `traceparent` continues
        # this trace instead of starting an unrelated one.
        inject(headers)

        logger.debug(
            "Publishing step: workflow_id=%s step_id=%s subject=%s",
            message.workflow_id,
            message.step_id,
            subject,
        )

        future = self._submit(
            self._request(subject, payload, headers, timeout),
            message.agent_id,
        )

        try:
            raw = future.result(timeout=timeout + _BRIDGE_TIMEOUT_MARGIN_SECONDS)
        except AgentTransportError:
            raise
        except TimeoutError as error:
            raise AgentTransportError(
                f"{message.agent_id} did not answer on {subject} within "
                f"{timeout:g}s"
            ) from error
        except Exception as error:
            raise AgentTransportError(
                f"{message.agent_id} request on {subject} failed: "
                f"{type(error).__name__}: {error}"
            ) from error

        try:
            decoded: Any = json.loads(raw)
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

    def close(self) -> None:
        """Drain the connection and stop the event-loop thread."""
        with self._lock:
            loop = self._loop
            connection = self._connection
            thread = self._thread

            self._loop = None
            self._connection = None
            self._thread = None

        if loop is None:
            return

        if connection is not None:
            try:
                asyncio.run_coroutine_threadsafe(connection.drain(), loop).result(
                    timeout=10
                )
            except Exception:
                logger.warning("Draining the NATS connection failed", exc_info=True)

        loop.call_soon_threadsafe(loop.stop)

        if thread is not None:
            thread.join(timeout=10)

    async def _request(
        self,
        subject: str,
        payload: bytes,
        headers: dict[str, str],
        timeout: float,
    ) -> bytes:
        """Send one request on the shared connection and return the reply body.

        Args:
            subject: Subject to publish on.
            payload: Encoded execution message.
            headers: NATS headers carrying routing and trace metadata.
            timeout: Seconds to wait for the reply.

        Returns:
            The raw reply body.
        """
        connection = await self._ensure_connection()
        reply = await connection.request(
            subject,
            payload,
            timeout=timeout,
            headers=headers,
        )

        return bytes(reply.data)

    async def _ensure_connection(self) -> Any:
        """Return the shared connection, opening it on first use."""
        if self._connection is not None:
            return self._connection

        import nats

        self._connection = await nats.connect(
            servers=self._servers,
            connect_timeout=self._connect_timeout,
        )
        logger.info("Connected to NATS at %s", ", ".join(self._servers))

        return self._connection

    def _submit(self, coroutine: Any, agent_id: str) -> "Future[bytes]":
        """Schedule a coroutine on the transport's event loop.

        Args:
            coroutine: The coroutine to run.
            agent_id: Used only for error messages.

        Returns:
            A future resolving to the coroutine's result.

        Raises:
            AgentTransportError: If the event loop could not be started.
        """
        loop = self._ensure_loop(agent_id)

        return asyncio.run_coroutine_threadsafe(coroutine, loop)

    def _ensure_loop(self, agent_id: str) -> asyncio.AbstractEventLoop:
        """Return the transport's event loop, starting its thread on first use.

        Args:
            agent_id: Used only for error messages.

        Returns:
            A running event loop owned by a daemon thread.

        Raises:
            AgentTransportError: If the loop did not start.
        """
        with self._lock:
            if self._loop is not None:
                return self._loop

            ready = threading.Event()
            loop = asyncio.new_event_loop()

            def run() -> None:
                asyncio.set_event_loop(loop)
                loop.call_soon(ready.set)
                loop.run_forever()

            thread = threading.Thread(target=run, name="kio1-nats", daemon=True)
            thread.start()

            if not ready.wait(timeout=10):
                raise AgentTransportError(
                    f"Could not start the NATS event loop for {agent_id}"
                )

            self._loop = loop
            self._thread = thread

            return loop
