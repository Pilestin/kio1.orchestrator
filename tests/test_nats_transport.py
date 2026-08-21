import asyncio
import json
from typing import Any
from urllib.error import HTTPError

import pytest

from core.config_loader import DispatchConfig
from dispatch.agent_registry import AgentEndpoint, AgentRegistry
from dispatch.agent_transport import AgentTransportError, HttpAgentTransport
from dispatch.nats_transport import NatsAgentTransport
from dispatch.transport_factory import build_transport
from dispatch.workflow_planner import ExecutionMessage

MESSAGE = ExecutionMessage(
    workflow_id="wf-nats01",
    step_id="s1",
    agent_id="KIO10",
    capability="energy_efficiency",
    endpoint="http://localhost:8110",
    task="Analyse the energy requirements",
)
AGENT = AgentEndpoint(
    agent_id="KIO10",
    endpoint="http://localhost:8110",
    path="/execute",
    capabilities=frozenset({"energy_efficiency"}),
)

REPLY = {
    "workflow_id": "wf-nats01",
    "step_id": "s1",
    "agent_id": "KIO10",
    "status": "ok",
    "output": {"summary": "edge deployment is feasible"},
    "error": None,
}


class FakeMessage:
    """Stands in for a NATS reply message."""

    def __init__(self, data: bytes) -> None:
        self.data = data


class FakeConnection:
    """Records requests and answers them from a canned reply."""

    def __init__(
        self,
        reply: bytes | Exception = b"",
    ) -> None:
        self.reply = reply
        self.requests: list[dict[str, Any]] = []
        self.drained = False

    async def request(
        self,
        subject: str,
        payload: bytes,
        timeout: float,
        headers: dict[str, str] | None = None,
    ) -> FakeMessage:
        self.requests.append(
            {
                "subject": subject,
                "payload": payload,
                "timeout": timeout,
                "headers": dict(headers or {}),
            }
        )

        if isinstance(self.reply, Exception):
            raise self.reply

        return FakeMessage(self.reply)

    async def drain(self) -> None:
        self.drained = True


@pytest.fixture
def connection(monkeypatch: pytest.MonkeyPatch) -> FakeConnection:
    """Replace `nats.connect` with a fake, and return the connection it hands out."""
    import nats

    fake = FakeConnection(json.dumps(REPLY).encode("utf-8"))

    async def connect(**kwargs: Any) -> FakeConnection:
        return fake

    monkeypatch.setattr(nats, "connect", connect)

    return fake


@pytest.fixture
def transport(connection: FakeConnection) -> Any:
    """Yield a transport wired to the fake connection, closed afterwards."""
    instance = NatsAgentTransport(["nats://localhost:4222"])

    yield instance

    instance.close()


# ── subjects ─────────────────────────────────────────────────────────────────


def test_the_subject_comes_from_the_template_by_default(transport: Any) -> None:
    assert transport.subject_for(AGENT) == "kio.kio10.execute"


def test_an_agent_can_name_its_own_subject(transport: Any) -> None:
    named = AgentEndpoint(
        agent_id="KIO10",
        endpoint="http://localhost:8110",
        path="/execute",
        capabilities=frozenset(),
        subject="ai4sweng.kio10.tasks",
    )

    assert transport.subject_for(named) == "ai4sweng.kio10.tasks"


def test_the_subject_template_is_configurable(connection: FakeConnection) -> None:
    transport = NatsAgentTransport(
        ["nats://localhost:4222"],
        subject_template="ai4sweng.{agent}.execute",
    )

    try:
        assert transport.subject_for(AGENT) == "ai4sweng.kio10.execute"
    finally:
        transport.close()


# ── the message body must not change between transports ──────────────────────


def test_the_nats_body_is_identical_to_the_http_body(
    transport: Any,
    connection: FakeConnection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The KIO1-KIO10 execution message is the contract. Changing transports
    # must not change a single byte of it.
    posted: dict[str, bytes] = {}

    def fake_urlopen(request: Any, timeout: float = 0) -> Any:
        posted["body"] = request.data
        raise HTTPError(request.full_url, 500, "stop here", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr("dispatch.agent_transport.urllib.request.urlopen", fake_urlopen)

    with pytest.raises(AgentTransportError):
        HttpAgentTransport().send(MESSAGE, AGENT, timeout=5.0)

    transport.send(MESSAGE, AGENT, timeout=5.0)

    assert connection.requests[0]["payload"] == posted["body"]
    assert json.loads(posted["body"]) == {
        "workflow_id": "wf-nats01",
        "step_id": "s1",
        "agent_id": "KIO10",
        "capability": "energy_efficiency",
        "endpoint": "http://localhost:8110",
        "task": "Analyse the energy requirements",
        "data": {},
    }


def test_routing_metadata_travels_in_headers_not_in_the_body(
    transport: Any,
    connection: FakeConnection,
) -> None:
    transport.send(MESSAGE, AGENT, timeout=5.0)

    request = connection.requests[0]
    assert request["subject"] == "kio.kio10.execute"
    assert request["headers"]["workflow_id"] == "wf-nats01"
    assert request["headers"]["step_id"] == "s1"
    assert request["headers"]["kio_id"] == "KIO10"
    assert set(json.loads(request["payload"])) == {
        "workflow_id",
        "step_id",
        "agent_id",
        "capability",
        "endpoint",
        "task",
        "data",
    }


# ── replies ──────────────────────────────────────────────────────────────────


def test_a_reply_is_decoded_and_returned(transport: Any) -> None:
    assert transport.send(MESSAGE, AGENT, timeout=5.0) == REPLY


def test_the_callers_timeout_is_passed_to_nats(
    transport: Any,
    connection: FakeConnection,
) -> None:
    transport.send(MESSAGE, AGENT, timeout=3.5)

    assert connection.requests[0]["timeout"] == 3.5


def test_a_non_json_reply_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import nats

    fake = FakeConnection(b"not json at all")

    async def connect(**kwargs: Any) -> FakeConnection:
        return fake

    monkeypatch.setattr(nats, "connect", connect)
    transport = NatsAgentTransport(["nats://localhost:4222"])

    try:
        with pytest.raises(AgentTransportError, match="non-JSON"):
            transport.send(MESSAGE, AGENT, timeout=5.0)
    finally:
        transport.close()


def test_a_reply_that_is_not_an_object_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nats

    fake = FakeConnection(b"[1, 2, 3]")

    async def connect(**kwargs: Any) -> FakeConnection:
        return fake

    monkeypatch.setattr(nats, "connect", connect)
    transport = NatsAgentTransport(["nats://localhost:4222"])

    try:
        with pytest.raises(AgentTransportError, match="expected a JSON object"):
            transport.send(MESSAGE, AGENT, timeout=5.0)
    finally:
        transport.close()


def test_a_nats_timeout_names_the_agent_and_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nats

    fake = FakeConnection(asyncio.TimeoutError())

    async def connect(**kwargs: Any) -> FakeConnection:
        return fake

    monkeypatch.setattr(nats, "connect", connect)
    transport = NatsAgentTransport(["nats://localhost:4222"])

    try:
        with pytest.raises(AgentTransportError) as raised:
            transport.send(MESSAGE, AGENT, timeout=1.0)
    finally:
        transport.close()

    assert "KIO10" in str(raised.value)
    assert "kio.kio10.execute" in str(raised.value)


def test_a_connection_failure_is_reported_as_a_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nats

    async def connect(**kwargs: Any) -> Any:
        raise OSError("no route to broker")

    monkeypatch.setattr(nats, "connect", connect)
    transport = NatsAgentTransport(["nats://localhost:4222"])

    try:
        with pytest.raises(AgentTransportError, match="no route to broker"):
            transport.send(MESSAGE, AGENT, timeout=1.0)
    finally:
        transport.close()


# ── connection reuse and shutdown ────────────────────────────────────────────


def test_one_connection_is_shared_across_dispatches(
    transport: Any,
    connection: FakeConnection,
) -> None:
    transport.send(MESSAGE, AGENT, timeout=5.0)
    transport.send(MESSAGE, AGENT, timeout=5.0)

    assert len(connection.requests) == 2


def test_close_drains_the_connection(connection: FakeConnection) -> None:
    transport = NatsAgentTransport(["nats://localhost:4222"])
    transport.send(MESSAGE, AGENT, timeout=5.0)

    transport.close()

    assert connection.drained is True


def test_close_is_safe_before_anything_was_sent() -> None:
    NatsAgentTransport(["nats://localhost:4222"]).close()


# ── transport selection ──────────────────────────────────────────────────────


def test_the_factory_returns_http_by_default() -> None:
    transport = build_transport(DispatchConfig(enabled=True))

    assert isinstance(transport, HttpAgentTransport)


def test_the_factory_returns_nats_when_configured() -> None:
    transport = build_transport(
        DispatchConfig(
            enabled=True,
            transport="nats",
            nats_servers=("nats://localhost:4222",),
            nats_subject_template="ai4sweng.{agent}.execute",
            registry=AgentRegistry({"KIO10": AGENT}),
        )
    )

    try:
        assert isinstance(transport, NatsAgentTransport)
        assert transport.subject_for(AGENT) == "ai4sweng.kio10.execute"
    finally:
        transport.close()


def test_the_http_transport_can_be_closed_too(monkeypatch: pytest.MonkeyPatch) -> None:
    # Callers hold a transport for a whole session and close it at the end,
    # so both implementations must accept close() without special-casing.
    build_transport(DispatchConfig(enabled=True)).close()
