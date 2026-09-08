import logging
from typing import Any

from core.config_loader import DispatchConfig
from dispatch.agent_registry import (
    KIND_CLI,
    KIND_HTTP,
    KIND_KIO,
    AgentEndpoint,
)
from dispatch.agent_transport import (
    AgentTransport,
    AgentTransportError,
    HttpAgentTransport,
)
from dispatch.nats_transport import NatsAgentTransport
from dispatch.tool_transport import CliToolTransport, HttpToolTransport
from dispatch.workflow_planner import ExecutionMessage

logger = logging.getLogger(__name__)


class RoutingTransport:
    """Sends each message with the transport its target's kind requires.

    `dispatch.transport` chooses how KIO *agents* are reached, HTTP or NATS.
    It says nothing about the other things a plan can address: a plain REST
    API is called over HTTP whatever the KIO transport is, and a command line
    tool is not reached over a network at all. Routing per entry rather than
    per configuration is what lets one plan mix all three.
    """

    def __init__(self, transports: dict[str, AgentTransport]) -> None:
        """Store the transport for each registry kind.

        Args:
            transports: Kind to transport mapping. A kind with no entry here
                is reported as a failed step rather than crashing the run.
        """
        self._transports = dict(transports)

    def transport_for(self, kind: str) -> AgentTransport | None:
        """Return the transport handling one registry kind.

        Args:
            kind: One of the registry kinds, such as `kio` or `cli`.

        Returns:
            The transport, or None when the kind is not routed.
        """
        return self._transports.get(kind)

    def send(
        self,
        message: ExecutionMessage,
        agent: AgentEndpoint,
        timeout: float,
    ) -> dict[str, Any]:
        """Deliver a message using the transport for the target's kind.

        Args:
            message: The execution message to deliver.
            agent: The resolved registry entry.
            timeout: Seconds to wait for the reply.

        Returns:
            The decoded reply.

        Raises:
            AgentTransportError: If no transport handles the entry's kind, or
                the chosen transport failed.
        """
        transport = self._transports.get(agent.kind)

        if transport is None:  # pragma: no cover - kinds are validated on load
            raise AgentTransportError(
                f"{agent.agent_id} is registered as kind {agent.kind!r}, which "
                "has no transport."
            )

        return transport.send(message, agent, timeout)

    def close(self) -> None:
        """Close every transport, so one failure cannot leak the others."""
        for transport in self._transports.values():
            try:
                transport.close()
            except Exception:
                logger.warning("A transport failed to close", exc_info=True)


def build_transport(dispatch_config: DispatchConfig) -> AgentTransport:
    """Return a transport able to reach every kind of registered target.

    KIO agents travel over the transport `dispatch.transport` selects, and
    both carry the same KIO1-KIO10 execution message. Registry entries that
    are not KIO agents are adapted by their own transports.

    Args:
        dispatch_config: Validated dispatch settings.

    Returns:
        A ready transport.
    """
    kio_transport: AgentTransport

    if dispatch_config.transport == "nats":
        logger.info(
            "Dispatching to KIO agents over NATS: servers=%s",
            ", ".join(dispatch_config.nats_servers),
        )
        kio_transport = NatsAgentTransport(
            list(dispatch_config.nats_servers),
            subject_template=dispatch_config.nats_subject_template,
        )
    else:
        logger.info("Dispatching to KIO agents over HTTP")
        kio_transport = HttpAgentTransport()

    return RoutingTransport(
        {
            KIND_KIO: kio_transport,
            KIND_HTTP: HttpToolTransport(),
            KIND_CLI: CliToolTransport(),
        }
    )
