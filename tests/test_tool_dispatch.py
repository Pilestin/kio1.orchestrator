"""Tests for dispatching to targets that are not KIO agents.

The point of these is that `workflow_executor` never learns the difference:
a local command and a plain REST API produce the same `StepResult` shape a
KIO agent does, including the `verdict` and `hitl_required` fields the
executor reads.
"""

import json
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar

import pytest

from core.config_loader import DispatchConfig
from core.prompt_loader import CATALOG_PLACEHOLDER, load_prompt, render_catalog
from dispatch.agent_registry import (
    KIND_CLI,
    KIND_HTTP,
    KIND_KIO,
    AgentRegistry,
    CliToolSpec,
    HttpToolSpec,
    load_agent_registry,
)
from dispatch.agent_transport import AgentTransportError
from dispatch.tool_transport import CliToolTransport, HttpToolTransport, render
from dispatch.transport_factory import build_transport
from dispatch.workflow_executor import STATUS_ERROR, STATUS_OK, execute_workflow
from dispatch.workflow_planner import ExecutionMessage, parse_plan, route_plan

MESSAGE = ExecutionMessage(
    workflow_id="wf-tool01",
    step_id="s1",
    agent_id="Pytest",
    capability="test_execution",
    endpoint="cli:Pytest",
    task="Run the suite",
    data={"repository": {"path": "/work/repo"}, "upstream": {"s0": {"n": 1}}},
)


def cli_registry(**overrides: Any) -> AgentRegistry:
    """Return a registry holding one CLI entry, with fields overridable."""
    entry: dict[str, Any] = {
        "type": "cli",
        "command": [sys.executable, "-c", "print('done')"],
        "capabilities": ["test_execution"],
    }
    entry.update(overrides)

    return load_agent_registry({"Pytest": entry})


def http_registry(**overrides: Any) -> AgentRegistry:
    """Return a registry holding one HTTP entry, with fields overridable."""
    entry: dict[str, Any] = {
        "type": "http",
        "url": "http://127.0.0.1:1/analyze",
        "capabilities": ["log_analysis"],
    }
    entry.update(overrides)

    return load_agent_registry({"LogAnalyzer": entry})


# ── placeholder substitution ─────────────────────────────────────────────────


def test_a_whole_string_placeholder_keeps_the_value_s_type() -> None:
    # "{data}" must yield an object, not its printed form, so a configured
    # body can forward the structured input unchanged.
    assert render("{data}", MESSAGE) == MESSAGE.data
    assert render("{data.upstream.s0}", MESSAGE) == {"n": 1}


def test_a_placeholder_inside_a_string_is_substituted_as_text() -> None:
    assert render("run: {task}", MESSAGE) == "run: Run the suite"
    assert render("--path={data.repository.path}", MESSAGE) == "--path=/work/repo"


def test_placeholders_are_substituted_through_lists_and_objects() -> None:
    rendered = render(
        {"cmd": ["--id", "{step_id}"], "meta": {"cap": "{capability}"}}, MESSAGE
    )

    assert rendered == {"cmd": ["--id", "s1"], "meta": {"cap": "test_execution"}}


def test_an_unknown_placeholder_is_left_alone() -> None:
    # A configured body may legitimately contain braces, and a typo should be
    # visible in the request rather than silently blanking the field.
    assert render("{not_a_field}", MESSAGE) == "{not_a_field}"
    assert render("{data.missing.key}", MESSAGE) == "{data.missing.key}"


# ── local commands ───────────────────────────────────────────────────────────


def test_a_command_that_succeeds_becomes_an_ok_step() -> None:
    registry = cli_registry()
    result = execute_workflow(
        parse_plan(plan_for("Pytest", "test_execution")),
        registry,
        build_transport(DispatchConfig(enabled=True, registry=registry)),
        timeout=30,
        max_parallel_steps=1,
    )

    assert result.succeeded == 1
    assert result.results[0].status == STATUS_OK
    assert result.results[0].output is not None
    assert result.results[0].output["stdout"] == "done"


def test_a_command_printing_json_reports_its_own_verdict() -> None:
    # This is what makes a local tool a first-class participant: it can say
    # what it concluded, exactly as a KIO agent does.
    registry = cli_registry(
        command=[
            sys.executable,
            "-c",
            (
                "import json; print(json.dumps("
                "{'verdict': 'defect', 'hitl_required': True, 'failed': 1}))"
            ),
        ]
    )
    result = execute_workflow(
        parse_plan(plan_for("Pytest", "test_execution")),
        registry,
        build_transport(DispatchConfig(enabled=True, registry=registry)),
        timeout=30,
        max_parallel_steps=1,
    )
    step = result.results[0]

    assert step.status == STATUS_OK
    assert step.verdict == "defect"
    assert step.review_required is True


def test_a_non_zero_exit_is_a_failed_step_not_a_broken_transport() -> None:
    registry = cli_registry(
        command=[
            sys.executable,
            "-c",
            "import sys; sys.stderr.write('2 tests failed'); sys.exit(1)",
        ]
    )
    result = execute_workflow(
        parse_plan(plan_for("Pytest", "test_execution")),
        registry,
        build_transport(DispatchConfig(enabled=True, registry=registry)),
        timeout=30,
        max_parallel_steps=1,
    )
    step = result.results[0]

    assert step.status == STATUS_ERROR
    assert step.error is not None
    assert "exited 1" in step.error
    assert "2 tests failed" in step.error


def test_the_task_text_is_one_argument_however_it_is_written() -> None:
    # The command runs without a shell, so nothing in a task description can
    # be read as syntax. The task here would be three commands under a shell.
    registry = cli_registry(
        command=[
            sys.executable,
            "-c",
            "import sys; print(len(sys.argv) - 1); print(sys.argv[1])",
            "{task}",
        ]
    )
    message = ExecutionMessage(
        workflow_id="wf-tool01",
        step_id="s1",
        agent_id="Pytest",
        capability="test_execution",
        endpoint="cli:Pytest",
        task="a; rm -rf / && echo `whoami`",
    )
    agent = registry.resolve("Pytest")
    assert agent is not None

    reply = CliToolTransport().send(message, agent, timeout=30)
    stdout = reply["output"]["stdout"].splitlines()

    assert stdout[0] == "1"
    assert stdout[1] == "a; rm -rf / && echo `whoami`"


def test_a_missing_program_says_so_plainly() -> None:
    registry = cli_registry(command=["kio1-no-such-program", "--version"])
    agent = registry.resolve("Pytest")
    assert agent is not None

    with pytest.raises(AgentTransportError, match="not found on this machine"):
        CliToolTransport().send(MESSAGE, agent, timeout=30)


def test_a_command_that_overruns_is_stopped() -> None:
    registry = cli_registry(
        command=[sys.executable, "-c", "import time; time.sleep(30)"]
    )
    agent = registry.resolve("Pytest")
    assert agent is not None

    with pytest.raises(AgentTransportError, match="did not finish within"):
        CliToolTransport().send(MESSAGE, agent, timeout=1)


def test_an_unset_environment_reference_fails_before_the_command_runs() -> None:
    registry = cli_registry(env={"TOKEN": "${KIO1_TEST_UNSET_TOKEN}"})
    agent = registry.resolve("Pytest")
    assert agent is not None

    with pytest.raises(AgentTransportError, match="KIO1_TEST_UNSET_TOKEN"):
        CliToolTransport().send(MESSAGE, agent, timeout=30)


# ── plain REST APIs ──────────────────────────────────────────────────────────


class RecordingHandler(BaseHTTPRequestHandler):
    """Captures one request and answers with whatever the test configured."""

    received: ClassVar[dict[str, Any]] = {}
    status_code = 200
    body = '{"findings": [], "verdict": "clean"}'
    content_type = "application/json"

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8") if length else ""

        type(self).received = {
            "path": self.path,
            "headers": dict(self.headers),
            "body": json.loads(raw) if raw else None,
        }
        self._respond()

    def do_GET(self) -> None:
        type(self).received = {"path": self.path, "headers": dict(self.headers)}
        self._respond()

    def _respond(self) -> None:
        payload = type(self).body.encode("utf-8")
        self.send_response(type(self).status_code)
        self.send_header("Content-Type", type(self).content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: Any) -> None:
        """Keep the test output clean."""


@pytest.fixture
def api() -> Iterator[str]:
    """Run a stub REST API and yield its base URL."""
    RecordingHandler.received = {}
    RecordingHandler.status_code = 200
    RecordingHandler.body = '{"findings": [], "verdict": "clean"}'
    RecordingHandler.content_type = "application/json"

    server = ThreadingHTTPServer(("127.0.0.1", 0), RecordingHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    try:
        yield f"http://127.0.0.1:{int(server.server_address[1])}"
    finally:
        server.shutdown()
        server.server_close()


def call_api(registry: AgentRegistry, agent_id: str = "LogAnalyzer") -> dict[str, Any]:
    """Send the standard message to a registered HTTP tool and return its reply."""
    agent = registry.resolve(agent_id)
    assert agent is not None

    return HttpToolTransport().send(MESSAGE, agent, timeout=10)


def test_an_api_without_a_configured_body_receives_the_execution_message(
    api: str,
) -> None:
    # An API that already speaks the protocol needs no mapping at all.
    reply = call_api(http_registry(url=f"{api}/analyze"))

    assert RecordingHandler.received["body"]["task"] == "Run the suite"
    assert RecordingHandler.received["body"]["step_id"] == "s1"
    assert reply["status"] == STATUS_OK
    assert reply["output"] == {"findings": [], "verdict": "clean"}


def test_a_configured_body_maps_the_message_into_the_api_s_own_shape(
    api: str,
) -> None:
    reply = call_api(
        http_registry(
            url=f"{api}/analyze",
            body={"query": "{task}", "repo": "{data.repository.path}", "top_k": 5},
        )
    )

    assert RecordingHandler.received["body"] == {
        "query": "Run the suite",
        "repo": "/work/repo",
        "top_k": 5,
    }
    assert reply["status"] == STATUS_OK


def test_result_path_keeps_only_the_part_that_matters(api: str) -> None:
    RecordingHandler.body = json.dumps(
        {"meta": {"ms": 12}, "data": {"result": {"verdict": "defect", "count": 3}}}
    )

    reply = call_api(
        http_registry(url=f"{api}/analyze", result_path=["data", "result"])
    )

    assert reply["output"] == {"verdict": "defect", "count": 3}


def test_a_plain_text_response_is_still_usable(api: str) -> None:
    RecordingHandler.body = "everything is fine"
    RecordingHandler.content_type = "text/plain"

    reply = call_api(http_registry(url=f"{api}/analyze"))

    assert reply["output"] == {"result": "everything is fine"}


def test_a_get_tool_sends_no_body(api: str) -> None:
    reply = call_api(http_registry(url=f"{api}/status", method="GET"))

    assert RecordingHandler.received["path"] == "/status"
    assert reply["status"] == STATUS_OK


def test_an_api_error_status_fails_the_step_with_the_body(api: str) -> None:
    RecordingHandler.status_code = 503
    RecordingHandler.body = '{"error": "index rebuilding"}'

    registry = http_registry(url=f"{api}/analyze")
    agent = registry.resolve("LogAnalyzer")
    assert agent is not None

    with pytest.raises(AgentTransportError, match="HTTP 503.*index rebuilding"):
        HttpToolTransport().send(MESSAGE, agent, timeout=10)


def test_a_header_reads_its_credential_from_the_environment(
    api: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Credentials belong in the environment, not in config.json.
    monkeypatch.setenv("KIO1_TEST_API_TOKEN", "s3cret")

    call_api(
        http_registry(
            url=f"{api}/analyze",
            headers={"Authorization": "Bearer ${KIO1_TEST_API_TOKEN}"},
        )
    )

    assert RecordingHandler.received["headers"]["Authorization"] == "Bearer s3cret"


def test_an_unset_credential_fails_before_the_call(api: str) -> None:
    registry = http_registry(
        url=f"{api}/analyze",
        headers={"Authorization": "Bearer ${KIO1_TEST_UNSET_TOKEN}"},
    )
    agent = registry.resolve("LogAnalyzer")
    assert agent is not None

    with pytest.raises(AgentTransportError, match="KIO1_TEST_UNSET_TOKEN"):
        HttpToolTransport().send(MESSAGE, agent, timeout=10)


# ── configuration ────────────────────────────────────────────────────────────


def test_an_entry_without_a_type_is_still_a_kio_agent() -> None:
    registry = load_agent_registry(
        {"KIO2": {"endpoint": "http://127.0.0.1:8102", "capabilities": ["diagnosis"]}}
    )
    agent = registry.resolve("KIO2")

    assert agent is not None
    assert agent.kind == KIND_KIO
    assert agent.tool is None
    assert agent.url == "http://127.0.0.1:8102/execute"


def test_a_cli_entry_needs_a_command_list() -> None:
    with pytest.raises(ValueError, match="command must be a non-empty list"):
        load_agent_registry({"Pytest": {"type": "cli", "capabilities": ["x"]}})


def test_a_cli_command_given_as_one_string_is_rejected() -> None:
    # Accepting a string would mean splitting it, and splitting is where shell
    # injection gets in.
    with pytest.raises(ValueError, match="command must be a non-empty list"):
        load_agent_registry({"Pytest": {"type": "cli", "command": "pytest -q"}})


def test_an_http_entry_needs_a_url() -> None:
    with pytest.raises(ValueError, match="url must be a non-empty HTTP"):
        load_agent_registry({"Api": {"type": "http", "capabilities": ["x"]}})


def test_an_unknown_type_is_rejected() -> None:
    with pytest.raises(ValueError, match="type must be one of"):
        load_agent_registry({"Thing": {"type": "grpc", "url": "http://x/y"}})


def test_the_repository_config_registers_local_tools() -> None:
    from core.config_loader import load_config

    registry = load_config("config.json").dispatch.registry
    pytest_tool = registry.resolve("Pytest")

    assert pytest_tool is not None
    assert pytest_tool.kind == KIND_CLI
    assert isinstance(pytest_tool.tool, CliToolSpec)
    assert "test_execution" in pytest_tool.capabilities


def test_configuration_shape_is_kept_per_kind() -> None:
    registry = load_agent_registry(
        {
            "Api": {
                "type": "http",
                "url": "https://example.test/v1/run",
                "method": "put",
                "headers": {"X-Key": "abc"},
                "capabilities": ["thing"],
            }
        }
    )
    agent = registry.resolve("Api")

    assert agent is not None
    assert agent.kind == KIND_HTTP
    assert isinstance(agent.tool, HttpToolSpec)
    assert agent.tool.method == "PUT"
    assert agent.tool.headers == {"X-Key": "abc"}


# ── the planner does not learn the difference ────────────────────────────────


def plan_for(agent_id: str, capability: str) -> dict[str, Any]:
    """Return a one-step plan addressed to a target."""
    return {
        "workflow_id": "wf-tool01",
        "execution_mode": "sequential",
        "steps": [
            {
                "step_id": "s1",
                "agent_id": agent_id,
                "capability": capability,
                "task": "Run the suite",
            }
        ],
        "explanation": "One step.",
    }


def test_a_tool_declaring_the_wrong_capability_is_skipped_like_any_agent() -> None:
    registry = cli_registry(capabilities=["lint"])
    routed = route_plan(parse_plan(plan_for("Pytest", "test_execution")), registry)

    assert not routed.messages
    assert routed.skipped[0].agent_id == "Pytest"


def test_one_plan_can_mix_a_kio_agent_and_a_local_command() -> None:
    from tools.dummy_agent import build_server, server_address

    server = build_server(agent_id="KIO11", host="127.0.0.1", port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server_address(server)

    registry = load_agent_registry(
        {
            "KIO11": {
                "endpoint": f"http://{host}:{port}",
                "path": "/execute",
                "capabilities": ["test_automation"],
            },
            "GitHistory": {
                "type": "cli",
                "command": [sys.executable, "-c", "print('abc123 first commit')"],
                "capabilities": ["repository_history"],
            },
        }
    )

    plan = parse_plan(
        {
            "workflow_id": "wf-mixed01",
            "execution_mode": "sequential",
            "steps": [
                {
                    "step_id": "s1",
                    "agent_id": "GitHistory",
                    "capability": "repository_history",
                    "task": "Read recent commits",
                },
                {
                    "step_id": "s2",
                    "agent_id": "KIO11",
                    "capability": "test_automation",
                    "task": "Run the tests",
                    "depends_on": ["s1"],
                },
            ],
            "explanation": "Mixed.",
        }
    )

    try:
        result = execute_workflow(
            plan,
            registry,
            build_transport(DispatchConfig(enabled=True, registry=registry)),
            timeout=30,
            max_parallel_steps=1,
        )
    finally:
        server.shutdown()
        server.server_close()

    assert result.succeeded == 2
    assert result.results[0].output is not None
    assert "abc123" in result.results[0].output["stdout"]
    # The KIO agent received the command's output as upstream input, with no
    # special handling for the fact that a shell command produced it.
    assert result.results[1].output is not None
    assert result.results[1].output["received_upstream"] == ["s1"]


# ── the generated catalog ────────────────────────────────────────────────────


def test_the_catalog_names_every_registered_target_and_its_kind() -> None:
    registry = load_agent_registry(
        {
            "KIO2": {
                "endpoint": "http://127.0.0.1:8102",
                "description": "Finds bugs.",
                "capabilities": ["diagnosis"],
            },
            "Pytest": {
                "type": "cli",
                "description": "Runs the suite.",
                "command": ["pytest"],
                "capabilities": ["test_execution"],
            },
        }
    )
    catalog = render_catalog(registry)

    assert "**KIO2** — Finds bugs." in catalog
    assert "**Pytest** _(local command)_ — Runs the suite." in catalog
    assert "`test_execution`" in catalog


def test_a_target_without_a_description_still_reads_sensibly() -> None:
    catalog = render_catalog(cli_registry())

    assert "Handles test_execution." in catalog


def test_an_empty_registry_says_so_rather_than_producing_a_blank(
    tmp_path: Any,
) -> None:
    assert "No targets are registered" in render_catalog(AgentRegistry({}))


def test_the_prompt_catalog_is_filled_from_the_registry(tmp_path: Any) -> None:
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text(
        f"Header\n\n{CATALOG_PLACEHOLDER}\n\nFooter", encoding="utf-8"
    )

    prompt = load_prompt(str(prompt_file), cli_registry())

    assert CATALOG_PLACEHOLDER not in prompt
    assert "**Pytest** _(local command)_" in prompt
    assert prompt.startswith("Header")
    assert prompt.endswith("Footer")


def test_a_prompt_without_the_placeholder_is_left_alone(tmp_path: Any) -> None:
    # Existing prompts keep working, and a hand-written catalog stays possible.
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("A prompt with its own catalog.", encoding="utf-8")

    assert load_prompt(str(prompt_file), cli_registry()) == (
        "A prompt with its own catalog."
    )


def test_the_shipped_prompt_asks_for_a_generated_catalog() -> None:
    from pathlib import Path

    prompt = Path("prompts/kio1_system_prompt_ver2.txt").read_text(encoding="utf-8")

    assert CATALOG_PLACEHOLDER in prompt


def test_the_summary_table_stays_aligned_for_long_target_names() -> None:
    # Target names used to be `KIO<N>`, so the column was six wide. A
    # registered command or API can be called anything.
    from dispatch.workflow_executor import StepResult, WorkflowResult, render_summary

    summary = render_summary(
        WorkflowResult(
            workflow_id="wf-x",
            execution_mode="sequential",
            results=(
                StepResult(
                    step_id="s1",
                    agent_id="GitHistory",
                    capability="repository_history",
                    status=STATUS_OK,
                    duration_ms=4,
                ),
                StepResult(
                    step_id="s2",
                    agent_id="KIO2",
                    capability="diagnosis",
                    status=STATUS_OK,
                    duration_ms=7,
                ),
            ),
        )
    )
    rows = summary.splitlines()[1:]
    status_columns = [row.index(STATUS_OK) for row in rows]

    assert status_columns[0] == status_columns[1]
