"""Carrying out a step with an agent that is a written role.

An `llm` target is instructions — "you review Python code for bugs" — and a
model to follow them. Nothing runs anywhere else: the step's task, its
arguments, and what earlier steps produced become the message, the
instructions become the system prompt, and the model's answer is the step's
output.

The answer is asked for as `{"result": "..."}`. Every provider here can be
held to JSON, and a small model keeps to a one-field shape far more reliably
than to free text it must not decorate; the field may hold Markdown. A reply
that is not JSON is kept whole as the result rather than lost.
"""

import json
import logging
import threading
from dataclasses import replace
from typing import Any

from core.config_loader import Config
from core.formatter import parse_response
from core.run_events import TokenRelay, emit, observed
from dispatch.agent_registry import AgentEndpoint, LlmToolSpec
from dispatch.agent_transport import AgentTransportError
from dispatch.tool_transport import protocol_reply
from dispatch.workflow_planner import ExecutionMessage
from providers.provider_client import load_provider, supports_streaming

logger = logging.getLogger(__name__)

# What a step's inputs may take up in the message. Upstream outputs can be
# long — a test run, a file — and the model reads every character.
_INPUT_CHARS = 8000

_CONTRACT = (
    "\n\n## How to answer\n"
    "You are carrying out one step of a larger plan. Respond ONLY with a JSON "
    'object: {"result": "<your answer>"}. The answer may use Markdown. If you '
    'judged something, you may add "verdict": "clean", "defect", or '
    '"inconclusive". Use only what the task and its inputs give you; say so '
    "when they are not enough."
)


def _clip(text: str) -> str:
    """Bound a step input, marking the cut."""
    if len(text) <= _INPUT_CHARS:
        return text

    return f"{text[:_INPUT_CHARS]}\n… [{len(text) - _INPUT_CHARS} more chars]"


def build_message(message: ExecutionMessage) -> str:
    """Return the user message for a step: the task and everything it got.

    Args:
        message: The step.

    Returns:
        The task, its capability, its arguments, and its other inputs —
        upstream results and the workspace — as readable sections.
    """
    sections = [f"## Task\n{message.task}", f"## Capability\n{message.capability}"]
    data = dict(message.data)
    arguments = data.pop("arguments", None)

    if arguments:
        sections.append(
            "## Arguments\n" + json.dumps(arguments, ensure_ascii=False, indent=2)
        )

    if data:
        sections.append(
            "## Inputs\n"
            + _clip(json.dumps(data, ensure_ascii=False, indent=2, default=str))
        )

    return "\n\n".join(sections)


def read_answer(content: str) -> dict[str, Any]:
    """Turn the model's reply into the step's output.

    Args:
        content: The reply.

    Returns:
        The decoded object when it carries a `result`; otherwise the reply
        itself under `result`, so nothing the model said is lost.
    """
    try:
        decoded = parse_response(content)
    except Exception:  # noqa: BLE001 - any undecodable reply is kept as text
        decoded = None

    if isinstance(decoded, dict) and "result" in decoded:
        return decoded

    return {"result": content.strip()}


class LlmTransport:
    """Runs `llm` targets with the configured models.

    Holds one client per provider, started on first use: an agent on the
    planner's provider shares nothing with the planner, but starting a client
    is cheap and keeping them apart means a settings change cannot leave a
    run holding a client for the wrong provider.
    """

    def __init__(self, config: Config) -> None:
        """Remember the live configuration.

        Args:
            config: The service's configuration object. Read at every step,
                so a model changed on the Settings tab is used from the next
                step on.
        """
        self._config = config
        self._clients: dict[str, tuple[Any, Any]] = {}
        self._lock = threading.Lock()

    def close(self) -> None:
        """Forget the clients; none hold connections open between calls."""
        self._clients.clear()

    def _model_for(self, spec: LlmToolSpec) -> tuple[Config, Any, Any]:
        """Return the configuration, provider module, and client for an agent."""
        config = replace(
            self._config,
            provider=spec.provider or self._config.provider,
            model=spec.model or self._config.model,
            temperature=(
                spec.temperature
                if spec.temperature is not None
                else self._config.temperature
            ),
        )

        with self._lock:
            cached = self._clients.get(config.provider)

            if cached is None:
                module = load_provider(config.provider, self._config.allowed_providers)
                cached = (module, module.create_client(config))
                self._clients[config.provider] = cached

        return config, cached[0], cached[1]

    def send(
        self, message: ExecutionMessage, agent: AgentEndpoint, timeout: float
    ) -> dict[str, Any]:
        """Carry out one step with the agent's role.

        Args:
            message: The step.
            agent: The `llm` target.
            timeout: The step's timeout. Not applied to the model call, which
                is bounded by the planning timeout instead: a local model on
                a CPU needs minutes, and the step timeout is sized for agents
                that answer in seconds.

        Returns:
            The reply in the KIO1-KIO10 shape.

        Raises:
            AgentTransportError: If the target is not an LLM agent, or the
                model could not be asked.
        """
        spec = agent.tool

        if not isinstance(spec, LlmToolSpec):
            raise AgentTransportError(
                f"{agent.agent_id} is not configured as an LLM agent."
            )

        try:
            config, provider, client = self._model_for(spec)
        except Exception as error:
            raise AgentTransportError(
                f"{agent.agent_id} could not start its model: {error}"
            ) from error

        streaming = observed() and supports_streaming(provider)
        relay = TokenRelay()

        if streaming:
            emit("model_output", channel="content", text=f"\n\n— {agent.agent_id} —\n")

        logger.info(
            "Running LLM agent: step_id=%s agent_id=%s model=%s/%s",
            message.step_id,
            agent.agent_id,
            config.provider,
            config.model,
        )

        try:
            response = provider.send_request(
                config=config,
                client=client,
                system_prompt=spec.instructions + _CONTRACT,
                messages=[{"role": "user", "content": build_message(message)}],
                **({"on_token": relay} if streaming else {}),
            )
            content = provider.extract_content(response)
        except Exception as error:
            raise AgentTransportError(
                f"{agent.agent_id}: the model did not answer: {type(error).__name__}: {error}"
            ) from error
        finally:
            relay.flush()

        return protocol_reply(message, status="ok", output=read_answer(content))
