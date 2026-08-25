import json
import threading
from collections.abc import Iterator
from typing import Any

import pytest

from core.config_loader import DispatchConfig, load_config
from dispatch.agent_registry import AgentEndpoint, AgentRegistry, load_agent_registry
from dispatch.agent_transport import AgentTransportError, HttpAgentTransport
from dispatch.workflow_executor import (
    STATUS_ERROR,
    STATUS_NOT_RUN,
    STATUS_OK,
    STATUS_SKIPPED,
    dispatch_plan,
    execute_workflow,
    render_summary,
)
from dispatch.workflow_planner import (
    SKIP_CAPABILITY_MISMATCH,
    SKIP_NO_ENDPOINT,
    ExecutionMessage,
    parse_plan,
    route_plan,
)
from tools.dummy_agent import build_server, server_address

KIO2_ENDPOINT = AgentEndpoint(
    agent_id="KIO2",
    endpoint="http://localhost:8102",
    path="/execute",
    capabilities=frozenset({"bug_localization", "fix_recommendation"}),
)
KIO10_ENDPOINT = AgentEndpoint(
    agent_id="KIO10",
    endpoint="http://localhost:8110",
    path="/execute",
    capabilities=frozenset({"energy_efficiency"}),
)


def build_plan_data(
    steps: list[dict[str, str]] | None = None,
    execution_mode: str = "sequential",
) -> dict[str, Any]:
    """Return a workflow plan object shaped like a model response."""
    return {
        "workflow_id": "wf-test01",
        "execution_mode": execution_mode,
        "steps": (
            steps
            if steps is not None
            else [
                {
                    "step_id": "s1",
                    "agent_id": "KIO2",
                    "capability": "bug_localization",
                    "task": "Locate the null dereference",
                }
            ]
        ),
        "explanation": "Test plan.",
    }


class RecordingTransport:
    """Transport stub that returns canned replies and records what it sent."""

    def __init__(self, replies: dict[str, Any] | None = None) -> None:
        self.replies = replies or {}
        self.sent: list[ExecutionMessage] = []
        self.lock = threading.Lock()

    def send(
        self,
        message: ExecutionMessage,
        agent: AgentEndpoint,
        timeout: float,
    ) -> dict[str, Any]:
        with self.lock:
            self.sent.append(message)

        reply = self.replies.get(message.step_id)

        if isinstance(reply, Exception):
            raise reply

        if reply is not None:
            return reply

        return {
            "workflow_id": message.workflow_id,
            "step_id": message.step_id,
            "agent_id": message.agent_id,
            "status": "ok",
            "output": {"summary": "done"},
            "error": None,
        }


@pytest.fixture
def registry() -> AgentRegistry:
    return AgentRegistry({"KIO2": KIO2_ENDPOINT, "KIO10": KIO10_ENDPOINT})


# ── agent registry ───────────────────────────────────────────────────────────


def test_load_agent_registry_reads_endpoint_and_capabilities() -> None:
    registry = load_agent_registry(
        {
            "KIO2": {
                "endpoint": "http://localhost:8102/",
                "capabilities": ["bug_localization"],
            }
        }
    )

    agent = registry.resolve("KIO2")

    assert agent is not None
    assert agent.endpoint == "http://localhost:8102"
    assert agent.path == "/execute"
    assert agent.url == "http://localhost:8102/execute"
    assert agent.accepts("bug_localization")
    assert not agent.accepts("energy_efficiency")


def test_load_agent_registry_treats_empty_capabilities_as_unrestricted() -> None:
    registry = load_agent_registry({"KIO2": {"endpoint": "http://localhost:8102"}})
    agent = registry.resolve("KIO2")

    assert agent is not None
    assert agent.accepts("anything_at_all")


def test_resolve_returns_none_for_undeployed_agent(registry: AgentRegistry) -> None:
    assert registry.resolve("KIO7") is None


@pytest.mark.parametrize(
    "agent_data",
    [
        {"endpoint": ""},
        {"endpoint": "localhost:8102"},
        {"endpoint": "ftp://localhost:8102"},
        {"endpoint": "http://localhost:8102", "path": "execute"},
        {"endpoint": "http://localhost:8102", "capabilities": "bug_localization"},
        {"endpoint": "http://localhost:8102", "capabilities": [""]},
    ],
)
def test_load_agent_registry_rejects_malformed_entries(
    agent_data: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        load_agent_registry({"KIO2": agent_data})


# ── plan parsing ─────────────────────────────────────────────────────────────


def test_parse_plan_accepts_a_well_formed_plan() -> None:
    plan = parse_plan(build_plan_data())

    assert plan.workflow_id == "wf-test01"
    assert plan.execution_mode == "sequential"
    assert len(plan.steps) == 1
    assert plan.steps[0].agent_id == "KIO2"


def test_parse_plan_strips_stray_whitespace_in_step_fields() -> None:
    # The shared KIO1-KIO10 protocol examples contain values such as
    # " energy_efficiency", so parsing must not depend on clean input.
    plan = parse_plan(
        build_plan_data(
            [
                {
                    "step_id": "s1",
                    "agent_id": "KIO10",
                    "capability": " energy_efficiency ",
                    "task": " Analyse energy use ",
                }
            ]
        )
    )

    assert plan.steps[0].capability == "energy_efficiency"
    assert plan.steps[0].task == "Analyse energy use"


@pytest.mark.parametrize(
    "mutation",
    [
        {"workflow_id": ""},
        {"execution_mode": "concurrent"},
        {"steps": []},
        {"steps": "s1"},
    ],
)
def test_parse_plan_rejects_invalid_plans(mutation: dict[str, Any]) -> None:
    plan_data = build_plan_data()
    plan_data.update(mutation)

    with pytest.raises(ValueError):
        parse_plan(plan_data)


def test_parse_plan_rejects_a_step_missing_a_required_field() -> None:
    with pytest.raises(ValueError, match="capability"):
        parse_plan(
            build_plan_data(
                [{"step_id": "s1", "agent_id": "KIO2", "task": "Locate the bug"}]
            )
        )


def test_parse_plan_rejects_duplicate_step_ids() -> None:
    step = {
        "step_id": "s1",
        "agent_id": "KIO2",
        "capability": "bug_localization",
        "task": "Locate the bug",
    }

    with pytest.raises(ValueError, match="Duplicate step_id"):
        parse_plan(build_plan_data([step, dict(step)]))


# ── routing ──────────────────────────────────────────────────────────────────


def test_route_plan_builds_the_protocol_execution_message(
    registry: AgentRegistry,
) -> None:
    routing = route_plan(parse_plan(build_plan_data()), registry)

    assert len(routing.messages) == 1
    assert routing.messages[0].to_payload() == {
        "workflow_id": "wf-test01",
        "step_id": "s1",
        "agent_id": "KIO2",
        "capability": "bug_localization",
        "endpoint": "http://localhost:8102",
        "task": "Locate the null dereference",
        "data": {},
    }


def test_route_plan_skips_agents_without_an_endpoint(registry: AgentRegistry) -> None:
    plan = parse_plan(
        build_plan_data(
            [
                {
                    "step_id": "s1",
                    "agent_id": "KIO7",
                    "capability": "code_generation",
                    "task": "Generate the fix",
                }
            ]
        )
    )

    routing = route_plan(plan, registry)

    assert routing.messages == ()
    assert routing.skipped[0].reason == SKIP_NO_ENDPOINT


def test_route_plan_skips_capabilities_the_agent_does_not_declare(
    registry: AgentRegistry,
) -> None:
    plan = parse_plan(
        build_plan_data(
            [
                {
                    "step_id": "s1",
                    "agent_id": "KIO2",
                    "capability": "energy_efficiency",
                    "task": "Wrong agent for this capability",
                }
            ]
        )
    )

    routing = route_plan(plan, registry)

    assert routing.messages == ()
    assert routing.skipped[0].reason == SKIP_CAPABILITY_MISMATCH


# ── execution ────────────────────────────────────────────────────────────────


def execute(
    plan_data: dict[str, Any],
    registry: AgentRegistry,
    transport: RecordingTransport,
    max_parallel_steps: int = 4,
) -> Any:
    return execute_workflow(
        parse_plan(plan_data),
        registry,
        transport,
        timeout=5.0,
        max_parallel_steps=max_parallel_steps,
    )


def test_execute_workflow_records_a_successful_step(registry: AgentRegistry) -> None:
    transport = RecordingTransport()

    result = execute(build_plan_data(), registry, transport)

    assert result.succeeded == 1
    assert result.failed == 0
    assert result.results[0].status == STATUS_OK
    assert result.results[0].output == {"summary": "done"}


def test_execute_workflow_reports_results_in_plan_order(
    registry: AgentRegistry,
) -> None:
    plan_data = build_plan_data(
        [
            {
                "step_id": "s1",
                "agent_id": "KIO7",
                "capability": "code_generation",
                "task": "Not deployed",
            },
            {
                "step_id": "s2",
                "agent_id": "KIO2",
                "capability": "bug_localization",
                "task": "Locate the bug",
            },
        ]
    )

    result = execute(plan_data, registry, RecordingTransport())

    assert [step.step_id for step in result.results] == ["s1", "s2"]
    assert result.results[0].status == STATUS_SKIPPED
    assert result.results[0].error == SKIP_NO_ENDPOINT
    assert result.results[1].status == STATUS_OK


def test_sequential_execution_halts_after_a_failed_step(
    registry: AgentRegistry,
) -> None:
    plan_data = build_plan_data(
        [
            {
                "step_id": "s1",
                "agent_id": "KIO2",
                "capability": "bug_localization",
                "task": "Locate the bug",
            },
            {
                "step_id": "s2",
                "agent_id": "KIO10",
                "capability": "energy_efficiency",
                "task": "Analyse energy use",
            },
        ]
    )
    transport = RecordingTransport({"s1": AgentTransportError("KIO2 is unreachable")})

    result = execute(plan_data, registry, transport)

    assert result.results[0].status == STATUS_ERROR
    assert result.results[1].status == STATUS_NOT_RUN
    assert [message.step_id for message in transport.sent] == ["s1"]


def test_parallel_execution_dispatches_every_step_despite_a_failure(
    registry: AgentRegistry,
) -> None:
    plan_data = build_plan_data(
        [
            {
                "step_id": "s1",
                "agent_id": "KIO2",
                "capability": "bug_localization",
                "task": "Locate the bug",
            },
            {
                "step_id": "s2",
                "agent_id": "KIO10",
                "capability": "energy_efficiency",
                "task": "Analyse energy use",
            },
        ],
        execution_mode="parallel",
    )
    transport = RecordingTransport({"s1": AgentTransportError("KIO2 is unreachable")})

    result = execute(plan_data, registry, transport)

    assert result.results[0].status == STATUS_ERROR
    assert result.results[1].status == STATUS_OK
    assert sorted(message.step_id for message in transport.sent) == ["s1", "s2"]


def test_execute_workflow_treats_mixed_mode_as_sequential(
    registry: AgentRegistry,
) -> None:
    plan_data = build_plan_data(execution_mode="mixed")

    result = execute(plan_data, registry, RecordingTransport())

    assert result.execution_mode == "mixed"
    assert result.results[0].status == STATUS_OK


def test_agent_reported_error_is_recorded_with_its_message(
    registry: AgentRegistry,
) -> None:
    transport = RecordingTransport(
        {
            "s1": {
                "workflow_id": "wf-test01",
                "step_id": "s1",
                "agent_id": "KIO2",
                "status": "error",
                "output": None,
                "error": "Repository not found",
            }
        }
    )

    result = execute(build_plan_data(), registry, transport)

    assert result.results[0].status == STATUS_ERROR
    assert result.results[0].error == "Repository not found"


def test_reply_without_status_is_treated_as_success(registry: AgentRegistry) -> None:
    transport = RecordingTransport(
        {"s1": {"workflow_id": "wf-test01", "step_id": "s1", "output": {"a": 1}}}
    )

    result = execute(build_plan_data(), registry, transport)

    assert result.results[0].status == STATUS_OK
    assert result.results[0].output == {"a": 1}


def test_correlation_mismatch_is_logged_but_does_not_fail_the_step(
    registry: AgentRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport = RecordingTransport(
        {
            "s1": {
                "workflow_id": "wf-somethingelse",
                "step_id": "s9",
                "status": "ok",
                "output": {},
            }
        }
    )

    with caplog.at_level("WARNING"):
        result = execute(build_plan_data(), registry, transport)

    assert result.results[0].status == STATUS_OK
    assert "workflow_id mismatch" in caplog.text
    assert "step_id mismatch" in caplog.text


def test_render_summary_lists_every_step(registry: AgentRegistry) -> None:
    result = execute(build_plan_data(), registry, RecordingTransport())

    summary = render_summary(result)

    assert "wf-test01" in summary
    assert "s1" in summary
    assert "KIO2" in summary


# ── dispatch_plan facade ─────────────────────────────────────────────────────


def test_dispatch_plan_returns_none_when_disabled(registry: AgentRegistry) -> None:
    config = DispatchConfig(enabled=False, registry=registry)

    assert dispatch_plan(build_plan_data(), config, RecordingTransport()) is None


def test_dispatch_plan_returns_none_for_an_unusable_plan(
    registry: AgentRegistry,
) -> None:
    config = DispatchConfig(enabled=True, registry=registry)

    assert dispatch_plan({"not": "a plan"}, config, RecordingTransport()) is None


def test_dispatch_plan_executes_a_valid_plan(registry: AgentRegistry) -> None:
    config = DispatchConfig(enabled=True, registry=registry)

    result = dispatch_plan(build_plan_data(), config, RecordingTransport())

    assert result is not None
    assert result.succeeded == 1


# ── configuration ────────────────────────────────────────────────────────────


def test_repository_config_json_is_valid_and_loads(tmp_path: Any) -> None:
    # config.json is parsed with json.loads, which rejects comments. This
    # guards against a comment being reintroduced into the shipped file.
    config = load_config("config.json")

    assert config.dispatch.registry.agent_ids() >= {"KIO2", "KIO10"}


def test_enabling_dispatch_without_agents_is_rejected(tmp_path: Any) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "provider": "ollama",
                "allowed_providers": ["ollama"],
                "model": "qwen2.5:3b",
                "prompt_path": "prompts/kio1_system_prompt_ver1.txt",
                "chat_directory": "chats",
                "temperature": 0.2,
                "request_timeout": 120,
                "dispatch": {"enabled": True, "agents": {}},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="nowhere to send"):
        load_config(str(config_path))


# ── end to end against the stub agent ────────────────────────────────────────


@pytest.fixture
def stub_agent() -> Iterator[AgentEndpoint]:
    """Serve a KIO2 stub on an ephemeral port for the duration of a test."""
    server = build_server(
        agent_id="KIO2",
        host="127.0.0.1",
        port=0,
        fail_capabilities=frozenset({"diagnosis"}),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    host, port = server_address(server)

    try:
        yield AgentEndpoint(
            agent_id="KIO2",
            endpoint=f"http://{host}:{port}",
            path="/execute",
            capabilities=frozenset({"bug_localization", "diagnosis"}),
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_end_to_end_dispatch_against_the_stub_agent(
    stub_agent: AgentEndpoint,
) -> None:
    registry = AgentRegistry({"KIO2": stub_agent})

    result = execute_workflow(
        parse_plan(build_plan_data()),
        registry,
        HttpAgentTransport(),
        timeout=5.0,
        max_parallel_steps=2,
    )

    assert result.succeeded == 1
    assert result.results[0].output is not None
    assert result.results[0].output["received_task"] == "Locate the null dereference"


def test_end_to_end_agent_error_is_surfaced(stub_agent: AgentEndpoint) -> None:
    registry = AgentRegistry({"KIO2": stub_agent})
    plan_data = build_plan_data(
        [
            {
                "step_id": "s1",
                "agent_id": "KIO2",
                "capability": "diagnosis",
                "task": "Diagnose the defect",
            }
        ]
    )

    result = execute_workflow(
        parse_plan(plan_data),
        registry,
        HttpAgentTransport(),
        timeout=5.0,
        max_parallel_steps=2,
    )

    assert result.results[0].status == STATUS_ERROR
    assert "Simulated failure" in (result.results[0].error or "")


def test_transport_reports_an_unreachable_agent() -> None:
    unreachable = AgentEndpoint(
        agent_id="KIO2",
        # Port 1 is reserved and never listening, so this fails fast.
        endpoint="http://127.0.0.1:1",
        path="/execute",
        capabilities=frozenset(),
    )
    message = ExecutionMessage(
        workflow_id="wf-test01",
        step_id="s1",
        agent_id="KIO2",
        capability="bug_localization",
        endpoint=unreachable.endpoint,
        task="Locate the bug",
    )

    with pytest.raises(AgentTransportError, match="unreachable"):
        HttpAgentTransport().send(message, unreachable, timeout=5.0)


def test_transport_reports_an_unknown_path(stub_agent: AgentEndpoint) -> None:
    wrong_path = AgentEndpoint(
        agent_id="KIO2",
        endpoint=stub_agent.endpoint,
        path="/does-not-exist",
        capabilities=frozenset(),
    )
    message = ExecutionMessage(
        workflow_id="wf-test01",
        step_id="s1",
        agent_id="KIO2",
        capability="bug_localization",
        endpoint=stub_agent.endpoint,
        task="Locate the bug",
    )

    with pytest.raises(AgentTransportError, match="HTTP 404"):
        HttpAgentTransport().send(message, wrong_path, timeout=5.0)


def test_an_unexpected_transport_error_fails_only_that_step(
    registry: AgentRegistry,
) -> None:
    # A transport is contracted to raise AgentTransportError. If it raises
    # anything else, the results of steps that already succeeded must survive.
    class BrokenTransport:
        def send(
            self,
            message: ExecutionMessage,
            agent: AgentEndpoint,
            timeout: float,
        ) -> dict[str, Any]:
            if message.step_id == "s2":
                raise RuntimeError("transport bug")

            return {
                "workflow_id": message.workflow_id,
                "step_id": message.step_id,
                "status": "ok",
                "output": {"summary": "done"},
            }

    plan_data = build_plan_data(
        [
            {
                "step_id": "s1",
                "agent_id": "KIO2",
                "capability": "bug_localization",
                "task": "Locate the bug",
            },
            {
                "step_id": "s2",
                "agent_id": "KIO10",
                "capability": "energy_efficiency",
                "task": "Analyse energy use",
            },
        ],
        execution_mode="parallel",
    )

    result = execute_workflow(
        parse_plan(plan_data),
        registry,
        BrokenTransport(),
        timeout=5.0,
        max_parallel_steps=2,
    )

    assert result.results[0].status == STATUS_OK
    assert result.results[1].status == STATUS_ERROR
    assert "RuntimeError: transport bug" == result.results[1].error


# ── the code under analysis, carried in the protocol's data field ───────────


def test_the_workspace_travels_in_the_data_field(registry: AgentRegistry) -> None:
    from core.config_loader import Workspace

    routing = route_plan(
        parse_plan(build_plan_data()),
        registry,
        workspace_data=Workspace(
            path="/work/repo", entry_point="reproduce.py"
        ).to_data(),
    )

    assert routing.messages[0].to_payload() == {
        "workflow_id": "wf-test01",
        "step_id": "s1",
        "agent_id": "KIO2",
        "capability": "bug_localization",
        "endpoint": "http://localhost:8102",
        "task": "Locate the null dereference",
        "data": {
            "repository": {"path": "/work/repo"},
            "target": {"entry_point": "reproduce.py"},
        },
    }


def test_an_unset_workspace_leaves_data_empty(registry: AgentRegistry) -> None:
    from core.config_loader import Workspace

    routing = route_plan(
        parse_plan(build_plan_data()),
        registry,
        workspace_data=Workspace().to_data(),
    )

    assert routing.messages[0].data == {}


def test_a_partially_set_workspace_only_sends_what_it_knows() -> None:
    from core.config_loader import Workspace

    assert Workspace(path="/work/repo").to_data() == {
        "repository": {"path": "/work/repo"}
    }
    assert Workspace(entry_point="reproduce.py").to_data() == {
        "target": {"entry_point": "reproduce.py"}
    }


def test_the_workspace_and_upstream_results_coexist(
    registry: AgentRegistry,
) -> None:
    # Both live under `data`, so one must not overwrite the other: an agent
    # needs the repository path and its predecessor's output together.
    from core.config_loader import DispatchConfig, Workspace

    transport = RecordingTransport()
    plan_data = build_plan_data(
        [
            {
                "step_id": "s1",
                "agent_id": "KIO2",
                "capability": "bug_localization",
                "task": "Locate the defect",
            },
            {
                "step_id": "s2",
                "agent_id": "KIO2",
                "capability": "fix_recommendation",
                "task": "Diagnose it",
                "depends_on": ["s1"],
            },
        ],
        execution_mode="mixed",
    )

    dispatch_plan(
        plan_data,
        DispatchConfig(
            enabled=True,
            request_timeout=5.0,
            max_parallel_steps=2,
            registry=registry,
            workspace=Workspace(path="/work/repo", entry_point="reproduce.py"),
        ),
        transport,
    )

    second = next(m for m in transport.sent if m.step_id == "s2")
    assert second.data["repository"] == {"path": "/work/repo"}
    assert second.data["target"] == {"entry_point": "reproduce.py"}
    assert second.data["upstream"] == {"s1": {"summary": "done"}}


def test_the_repository_config_registers_kio2_without_fix_recommendation() -> None:
    # KIO2 refuses fix_recommendation: per D2.6 UC-UC1-03 the patch is KIO7's
    # step. Registering it would send KIO2 work it answers with an error.
    config = load_config("config.json")
    kio2 = config.dispatch.registry.resolve("KIO2")

    assert kio2 is not None
    assert "fix_recommendation" not in kio2.capabilities
    assert {"bug_localization", "diagnosis"} <= kio2.capabilities


# ── per-deployment addresses come from the environment ──────────────────────


def make_config_file(tmp_path: Any) -> str:
    """Write a configuration naming addresses on the local machine."""
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "provider": "ollama",
                "allowed_providers": ["ollama"],
                "model": "test-model",
                "prompt_path": "prompts/test.txt",
                "chat_directory": "chats",
                "temperature": 0.2,
                "request_timeout": 120,
                "provider_options": {"endpoint": "http://127.0.0.1:11434"},
                "dispatch": {
                    "enabled": True,
                    "agents": {
                        "KIO2": {"endpoint": "http://127.0.0.1:8102"},
                        "KIO10": {"endpoint": "http://127.0.0.1:8110"},
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    return str(config_path)


def test_the_provider_endpoint_can_be_overridden(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Inside a container 127.0.0.1 is the container itself, so a committed
    # config cannot name the model host for every deployment.
    monkeypatch.setenv("KIO1_PROVIDER_ENDPOINT", "http://host.docker.internal:11434")

    config = load_config(make_config_file(tmp_path))

    assert config.provider_options["endpoint"] == "http://host.docker.internal:11434"


def test_one_agent_endpoint_can_be_overridden_without_touching_the_others(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KIO1_AGENT_ENDPOINT_KIO2", "http://host.docker.internal:8102")

    config = load_config(make_config_file(tmp_path))
    kio2 = config.dispatch.registry.resolve("KIO2")
    kio10 = config.dispatch.registry.resolve("KIO10")

    assert kio2 is not None and kio2.endpoint == "http://host.docker.internal:8102"
    assert kio10 is not None and kio10.endpoint == "http://127.0.0.1:8110"


def test_an_empty_override_is_ignored(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KIO1_PROVIDER_ENDPOINT", "   ")
    monkeypatch.setenv("KIO1_AGENT_ENDPOINT_KIO2", "")

    config = load_config(make_config_file(tmp_path))
    kio2 = config.dispatch.registry.resolve("KIO2")

    assert config.provider_options["endpoint"] == "http://127.0.0.1:11434"
    assert kio2 is not None and kio2.endpoint == "http://127.0.0.1:8102"


def test_without_overrides_the_file_is_used(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KIO1_PROVIDER_ENDPOINT", raising=False)
    monkeypatch.delenv("KIO1_AGENT_ENDPOINT_KIO2", raising=False)

    config = load_config(make_config_file(tmp_path))
    kio2 = config.dispatch.registry.resolve("KIO2")

    assert config.provider_options["endpoint"] == "http://127.0.0.1:11434"
    assert kio2 is not None and kio2.endpoint == "http://127.0.0.1:8102"


# ── what an agent concluded, separate from whether the call worked ──────────


def reply_with(output: dict[str, Any] | None) -> dict[str, Any]:
    """Build a successful agent reply carrying the given output."""
    return {
        "workflow_id": "wf-test01",
        "step_id": "s1",
        "agent_id": "KIO2",
        "status": "ok",
        "output": output,
        "error": None,
    }


@pytest.mark.parametrize("verdict", ["defect", "clean", "inconclusive"])
def test_a_reported_verdict_is_carried_on_the_result(
    registry: AgentRegistry, verdict: str
) -> None:
    transport = RecordingTransport({"s1": reply_with({"verdict": verdict})})

    result = execute(build_plan_data(), registry, transport)

    assert result.results[0].status == STATUS_OK
    assert result.results[0].verdict == verdict


def test_a_clean_verdict_is_still_a_successful_step(
    registry: AgentRegistry,
) -> None:
    # An agent that reports code is fine has succeeded. Treating that as a
    # failure would fail every workflow run over healthy code.
    transport = RecordingTransport(
        {"s1": reply_with({"verdict": "clean", "defect_found": False, "findings": []})}
    )

    result = execute(build_plan_data(), registry, transport)

    assert result.succeeded == 1
    assert result.failed == 0
    assert result.results[0].verdict == "clean"


def test_an_agent_that_reports_no_verdict_is_unaffected(
    registry: AgentRegistry,
) -> None:
    result = execute(build_plan_data(), registry, RecordingTransport())

    assert result.results[0].status == STATUS_OK
    assert result.results[0].verdict is None
    assert result.results[0].review_required is False


@pytest.mark.parametrize("bad", ["maybe", "", 3, None, ["defect"]])
def test_an_unrecognized_verdict_is_ignored(registry: AgentRegistry, bad: Any) -> None:
    transport = RecordingTransport({"s1": reply_with({"verdict": bad})})

    result = execute(build_plan_data(), registry, transport)

    assert result.results[0].status == STATUS_OK
    assert result.results[0].verdict is None


def test_a_request_for_human_review_is_carried(registry: AgentRegistry) -> None:
    transport = RecordingTransport(
        {"s1": reply_with({"verdict": "defect", "hitl_required": True})}
    )

    result = execute(build_plan_data(), registry, transport)

    assert result.results[0].review_required is True


def test_a_non_boolean_review_flag_is_not_treated_as_a_request(
    registry: AgentRegistry,
) -> None:
    transport = RecordingTransport({"s1": reply_with({"hitl_required": "yes"})})

    result = execute(build_plan_data(), registry, transport)

    assert result.results[0].review_required is False


def test_the_summary_shows_the_verdict_and_the_review_request(
    registry: AgentRegistry,
) -> None:
    # Without this, "nothing wrong here" and "found the defect" print
    # identically as `ok`.
    transport = RecordingTransport(
        {"s1": reply_with({"verdict": "clean", "hitl_required": True})}
    )

    summary = render_summary(execute(build_plan_data(), registry, transport))

    assert "clean" in summary
    assert "review required" in summary


def test_one_variable_rehosts_every_agent(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Agents usually move together, from a laptop's loopback to the Docker
    # host. Without this, registering another agent means remembering to add
    # another variable, and forgetting it fails only at dispatch time.
    monkeypatch.setenv("KIO1_AGENT_HOST", "host.docker.internal")

    config = load_config(make_config_file(tmp_path))
    endpoints = {
        agent_id: config.dispatch.registry.resolve(agent_id).endpoint  # type: ignore[union-attr]
        for agent_id in ("KIO2", "KIO10")
    }

    assert endpoints == {
        "KIO2": "http://host.docker.internal:8102",
        "KIO10": "http://host.docker.internal:8110",
    }


def test_naming_one_agent_wins_over_the_shared_host(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KIO1_AGENT_HOST", "host.docker.internal")
    monkeypatch.setenv("KIO1_AGENT_ENDPOINT_KIO2", "http://elsewhere:9999")

    config = load_config(make_config_file(tmp_path))
    kio2 = config.dispatch.registry.resolve("KIO2")
    kio10 = config.dispatch.registry.resolve("KIO10")

    assert kio2 is not None and kio2.endpoint == "http://elsewhere:9999"
    assert kio10 is not None and kio10.endpoint == "http://host.docker.internal:8110"


def test_rehosting_keeps_the_port_and_scheme(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "provider": "ollama",
                "allowed_providers": ["ollama"],
                "model": "test-model",
                "prompt_path": "prompts/test.txt",
                "chat_directory": "chats",
                "temperature": 0.2,
                "request_timeout": 120,
                "dispatch": {"agents": {"KIO2": {"endpoint": "https://old-host:8443"}}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("KIO1_AGENT_HOST", "new-host")

    kio2 = load_config(str(config_path)).dispatch.registry.resolve("KIO2")

    assert kio2 is not None and kio2.endpoint == "https://new-host:8443"


def test_without_the_variable_agents_stay_where_the_file_puts_them(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KIO1_AGENT_HOST", raising=False)

    config = load_config(make_config_file(tmp_path))
    kio2 = config.dispatch.registry.resolve("KIO2")

    assert kio2 is not None and kio2.endpoint == "http://127.0.0.1:8102"
