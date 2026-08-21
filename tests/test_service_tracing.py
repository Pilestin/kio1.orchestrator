import json
from collections.abc import Iterator, Mapping
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from core.config_loader import Config, DispatchConfig, TelemetryConfig
from core.service_telemetry import TraceContextMiddleware, init_service_tracing
from core.session_client import SessionClient
from dispatch.agent_registry import AgentEndpoint, AgentRegistry
from dispatch.agent_transport import HttpAgentTransport
from services.session_manager.app import app as session_manager_app
from services.session_manager.app import get_repository
from services.session_manager.repository import InMemorySessionRepository
from services.workflow_api.app import _State
from services.workflow_api.app import app as workflow_app
from services.workflow_api.app import get_state
from services.workflow_api.runner import WorkflowRunner
from tests.test_services_integration import _serve
from tools.dummy_agent import build_server, server_address


def _install_provider(exporter: InMemorySpanExporter) -> TracerProvider:
    """Force a fresh global tracer provider that records into `exporter`.

    OpenTelemetry allows the global provider to be set once per process, so a
    test that needs its own has to clear that latch. This reaches into private
    state deliberately and only here.

    Args:
        exporter: Collects every span produced during a test.

    Returns:
        The installed provider.
    """
    from opentelemetry.util._once import Once

    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    trace._TRACER_PROVIDER_SET_ONCE = Once()  # type: ignore[attr-defined]
    trace._TRACER_PROVIDER = None  # type: ignore[attr-defined]
    trace.set_tracer_provider(provider)

    return provider


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    """Yield an exporter collecting every span produced during a test."""
    exporter = InMemorySpanExporter()
    provider = _install_provider(exporter)

    try:
        yield exporter
    finally:
        provider.shutdown()


def by_name(exporter: InMemorySpanExporter) -> dict[str, ReadableSpan]:
    """Return the finished spans keyed by name."""
    return {span.name: span for span in exporter.get_finished_spans()}


def attributes_of(span: ReadableSpan) -> Mapping[str, Any]:
    """Return a span's attributes, failing the test when it has none.

    Args:
        span: The span to read.

    Returns:
        The attribute mapping.
    """
    assert span.attributes is not None, f"{span.name} recorded no attributes"

    return span.attributes


# ── the middleware on its own ───────────────────────────────────────────────


@pytest.fixture
def traced_app() -> FastAPI:
    """Return a small application wrapped in the middleware."""
    app = FastAPI()

    @app.get("/items/{item_id}")
    def read_item(item_id: str) -> dict[str, str]:
        return {"item_id": item_id}

    @app.get("/sessions/{session_id}")
    def read_session(session_id: str) -> dict[str, str]:
        return {"session_id": session_id}

    @app.get("/broken")
    def broken() -> None:
        raise HTTPException(status_code=500, detail="boom")

    app.add_middleware(TraceContextMiddleware, service_name="test-service")

    return app


def test_a_request_produces_one_server_span(
    traced_app: FastAPI, spans: InMemorySpanExporter
) -> None:
    TestClient(traced_app).get("/items/abc")

    finished = spans.get_finished_spans()
    assert len(finished) == 1
    span = finished[0]
    assert span.kind is SpanKind.SERVER
    assert attributes_of(span)["http.request.method"] == "GET"
    assert attributes_of(span)["url.path"] == "/items/abc"
    assert attributes_of(span)["http.response.status_code"] == 200
    assert attributes_of(span)["service.name"] == "test-service"


def test_the_span_name_uses_the_route_not_the_raw_path(
    traced_app: FastAPI, spans: InMemorySpanExporter
) -> None:
    # A raw path would put an identifier in the span name, and span names must
    # stay low-cardinality.
    TestClient(traced_app).get("/items/abc")

    assert spans.get_finished_spans()[0].name == "GET /items/{item_id}"


def test_a_session_id_in_the_path_becomes_an_attribute(
    traced_app: FastAPI, spans: InMemorySpanExporter
) -> None:
    TestClient(traced_app).get("/sessions/1c1d0e1f-0000-4000-8000-000000000000")

    span = spans.get_finished_spans()[0]
    assert span.name == "GET /sessions/{session_id}"
    assert attributes_of(span)["session.id"] == "1c1d0e1f-0000-4000-8000-000000000000"


def test_a_server_error_marks_the_span_as_failed(
    traced_app: FastAPI, spans: InMemorySpanExporter
) -> None:
    TestClient(traced_app).get("/broken")

    span = spans.get_finished_spans()[0]
    assert attributes_of(span)["http.response.status_code"] == 500
    assert span.status.is_ok is False


def test_an_incoming_traceparent_is_continued(
    traced_app: FastAPI, spans: InMemorySpanExporter
) -> None:
    trace_id = "0af7651916cd43dd8448eb211c80319c"
    parent_span_id = "b7ad6b7169203331"

    TestClient(traced_app).get(
        "/items/abc",
        headers={"traceparent": f"00-{trace_id}-{parent_span_id}-01"},
    )

    span = spans.get_finished_spans()[0]
    assert format(span.context.trace_id, "032x") == trace_id
    assert span.parent is not None
    assert format(span.parent.span_id, "016x") == parent_span_id


def test_a_request_without_traceparent_starts_its_own_trace(
    traced_app: FastAPI, spans: InMemorySpanExporter
) -> None:
    TestClient(traced_app).get("/items/abc")

    assert spans.get_finished_spans()[0].parent is None


# ── tracing setup ───────────────────────────────────────────────────────────


def test_disabled_telemetry_installs_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[Any] = []
    monkeypatch.setattr(
        "core.service_telemetry.TracerProvider",
        lambda **kwargs: created.append(kwargs),
    )

    init_service_tracing(TelemetryConfig(enabled=False), service_name="x")

    assert created == []


# ── the whole chain, in one trace ───────────────────────────────────────────

PLAN = {
    "workflow_id": "wf-trace01",
    "execution_mode": "sequential",
    "steps": [
        {
            "step_id": "s1",
            "agent_id": "KIO2",
            "capability": "bug_localization",
            "task": "Locate the defect",
        }
    ],
    "explanation": "One step.",
}


class PlanningProvider:
    """Returns a fixed plan, standing in for a real model provider."""

    def send_request(self, **kwargs: Any) -> dict[str, Any]:
        return {"content": json.dumps(PLAN)}

    def extract_content(self, response: dict[str, Any]) -> str:
        return str(response["content"])


@pytest.fixture
def session_manager_url() -> Iterator[str]:
    """Serve the Session Manager over real HTTP with in-memory storage."""
    repository = InMemorySessionRepository()
    session_manager_app.dependency_overrides[get_repository] = lambda: repository

    try:
        yield from _serve(session_manager_app)
    finally:
        session_manager_app.dependency_overrides.clear()


@pytest.fixture
def kio2_endpoint() -> Iterator[AgentEndpoint]:
    """Serve a KIO2 stub agent on an ephemeral port."""
    server = build_server(agent_id="KIO2", host="127.0.0.1", port=0)
    import threading

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server_address(server)

    try:
        yield AgentEndpoint(
            agent_id="KIO2",
            endpoint=f"http://{host}:{port}",
            path="/execute",
            capabilities=frozenset({"bug_localization"}),
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_one_request_produces_a_single_connected_trace(
    spans: InMemorySpanExporter,
    session_manager_url: str,
    kio2_endpoint: AgentEndpoint,
) -> None:
    """The whole path must share one trace id, across two services.

    This is the point of the exercise: before the Session Manager read the
    incoming `traceparent`, its work started an unconnected trace and the
    end-to-end view stopped at the Workflow API boundary.
    """
    config = Config(
        provider="ollama",
        allowed_providers=frozenset({"ollama"}),
        model="test-model",
        prompt_path="prompts/test.txt",
        chat_directory="chats",
        temperature=0.2,
        request_timeout=30,
        keep_alive=-1,
        max_output_tokens=1024,
        dispatch=DispatchConfig(
            enabled=True,
            request_timeout=10.0,
            max_parallel_steps=2,
            registry=AgentRegistry({"KIO2": kio2_endpoint}),
        ),
    )
    session_client = SessionClient(session_manager_url)
    workflow_app.dependency_overrides[get_state] = lambda: _State(
        runner=WorkflowRunner(
            config=config,
            system_prompt="You are KIO1.",
            provider=PlanningProvider(),
            provider_client=None,
            recorder=session_client,
            transport=HttpAgentTransport(),
        ),
        session_client=session_client,
    )

    try:
        response = TestClient(workflow_app).post(
            "/workflow/run", json={"request": "Fix the payment bug"}
        )
    finally:
        workflow_app.dependency_overrides.clear()

    assert response.status_code == 202

    finished = spans.get_finished_spans()
    names = {span.name for span in finished}

    # The Workflow API's own request, the orchestration, the dispatch to the
    # agent, and the Session Manager's writes.
    assert "POST /workflow/run" in names
    assert "kio1.workflow.run" in names
    assert "dispatch KIO2" in names
    assert "POST /sessions" in names
    assert "POST /sessions/{session_id}/steps" in names

    trace_ids = {span.context.trace_id for span in finished}
    assert len(trace_ids) == 1, (
        "every span must belong to one trace: "
        f"{sorted((span.name, format(span.context.trace_id, '032x')) for span in finished)}"
    )


def test_the_session_manager_records_the_session_id_on_its_spans(
    spans: InMemorySpanExporter,
    session_manager_url: str,
) -> None:
    client = SessionClient(session_manager_url)
    session_id = client.create_session("Fix the payment bug")

    client.record_step(
        session_id,
        {
            "step_id": "s1",
            "agent_id": "KIO2",
            "capability": "bug_localization",
            "task": "Locate the defect",
            "status": "ok",
            "output": {"summary": "found"},
        },
    )

    step_span = by_name(spans)["POST /sessions/{session_id}/steps"]
    assert attributes_of(step_span)["session.id"] == str(session_id)
