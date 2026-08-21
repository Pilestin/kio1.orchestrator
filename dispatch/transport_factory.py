import logging

from core.config_loader import DispatchConfig
from dispatch.agent_transport import AgentTransport, HttpAgentTransport
from dispatch.nats_transport import NatsAgentTransport

logger = logging.getLogger(__name__)


def build_transport(dispatch_config: DispatchConfig) -> AgentTransport:
    """Return the transport the configuration selects.

    Both transports carry the same KIO1-KIO10 execution message; only the
    delivery mechanism differs, so callers never branch on the choice.

    Args:
        dispatch_config: Validated dispatch settings.

    Returns:
        A ready transport.
    """
    if dispatch_config.transport == "nats":
        logger.info(
            "Dispatching over NATS: servers=%s",
            ", ".join(dispatch_config.nats_servers),
        )
        return NatsAgentTransport(
            list(dispatch_config.nats_servers),
            subject_template=dispatch_config.nats_subject_template,
        )

    logger.info("Dispatching over HTTP")

    return HttpAgentTransport()
