"""Tests for Model Context Protocol targets.

Three things are worth holding still here, and they are the three that make
the kind worth having:

- a server is *asked* what it can do, and the answer becomes the entry's
  capabilities, so nobody types a tool name into `config.json`;
- a plan step's free-text task is turned into structured arguments only where
  that is unambiguous, and refuses clearly where it is not;
- a server that dies does not take its target with it permanently.

The stdio tests run a real child process — a small Python script speaking the
protocol on its pipes. A mock of the pipes would pass while the framing,
buffering, and process handling stayed wrong, which is the whole hard part.
"""

import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from dispatch.agent_registry import McpToolSpec, load_agent_entry
from dispatch.agent_transport import AgentTransportError
from dispatch.directory import (
    STATUS_READY,
    STATUS_UNREACHABLE,
    AgentDirectory,
    AgentRegistry,
    HealthConfig,
    probe_target,
)
from dispatch.mcp_client import (
    McpError,
    McpSessionPool,
    McpTool,
    StdioMcpSession,
    flatten_content,
)
from dispatch.mcp_transport import McpToolTransport, arguments_for
from dispatch.workflow_planner import ExecutionMessage

HEALTH = HealthConfig(enabled=True, timeout=15.0)


# ── a real MCP server, small enough to read ─────────────────────────────────

SERVER = """
import json
import sys

TOOLS = [
    {
        "name": "echo",
        "description": "Repeat a message back.",
        "inputSchema": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
    },
    {
        "name": "add",
        "description": "Add two numbers.",
        "inputSchema": {
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
        },
    },
    {
        "name": "ping",
        "description": "Answer with pong.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

# Servers really do write to stderr before saying anything on stdout, and a
# client that does not drain it deadlocks once the pipe buffer fills.
for _ in range(200):
    print("starting up, please wait", file=sys.stderr)
sys.stderr.flush()
print("this is a banner, not JSON", flush=True)


def handle(request):
    method = request.get("method")
    params = request.get("params") or {}

    if method == "initialize":
        return {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "tiny", "version": "1"},
        }

    if method == "tools/list":
        return {"tools": TOOLS}

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}

        if name == "echo":
            return {"content": [{"type": "text", "text": arguments["message"]}]}

        if name == "add":
            total = arguments["a"] + arguments["b"]
            return {
                "content": [{"type": "text", "text": str(total)}],
                "structuredContent": {"total": total},
            }

        if name == "ping":
            return {"content": [{"type": "text", "text": "pong"}]}

        if name == "explode":
            return {"content": [{"type": "text", "text": "it broke"}], "isError": True}

        return None

    return None


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue

    message = json.loads(line)

    if "id" not in message:
        # A notification; nothing is answered.
        continue

    result = handle(message)

    if result is None:
        reply = {
            "jsonrpc": "2.0",
            "id": message["id"],
            "error": {"code": -32601, "message": "no such method or tool"},
        }
    else:
        reply = {"jsonrpc": "2.0", "id": message["id"], "result": result}

    print(json.dumps(reply), flush=True)
"""


@pytest.fixture
def server(tmp_path: Path) -> list[str]:
    """Return the command that runs the tiny server above."""
    script = tmp_path / "tiny_mcp_server.py"
    script.write_text(SERVER, encoding="utf-8")

    return [sys.executable, str(script)]


@pytest.fixture
def pool() -> Any:
    """Return a session pool that is closed when the test ends."""
    opened = McpSessionPool(timeout=15.0)

    yield opened

    opened.close()


def entry_for(command: list[str], **extra: Any) -> Any:
    """Return a registry entry for a stdio MCP server."""
    return load_agent_entry("Tiny", {"type": "mcp", "command": command, **extra})


def message(capability: str, task: str = "do it", **data: Any) -> ExecutionMessage:
    """Return an execution message addressed to the tiny server."""
    return ExecutionMessage(
        workflow_id="wf-1",
        step_id="s1",
        agent_id="Tiny",
        capability=capability,
        endpoint="mcp:Tiny",
        task=task,
        data=dict(data),
    )


# ── talking to a real server ────────────────────────────────────────────────


def test_a_server_says_what_it_can_do(server: list[str], pool: Any) -> None:
    session = StdioMcpSession("Tiny", tuple(server), timeout=15.0)
    pool._sessions["Tiny"] = session

    tools = session.list_tools()

    assert [tool.name for tool in tools] == ["echo", "add", "ping"]
    assert tools[0].required == ("message",)
    assert tools[1].properties["a"]["type"] == "integer"


def test_a_banner_before_the_protocol_is_not_fatal(
    server: list[str], pool: Any
) -> None:
    # The fixture server prints a non-JSON line first, which several real
    # servers do. Skipping it rather than failing is the difference between
    # "works" and "works with the popular ones".
    session = StdioMcpSession("Tiny", tuple(server), timeout=15.0)
    pool._sessions["Tiny"] = session

    assert session.list_tools()


def test_a_chatty_server_does_not_deadlock(server: list[str], pool: Any) -> None:
    # 200 lines on stderr is more than a pipe buffer holds. Without a thread
    # draining it the server blocks writing and this call never returns.
    session = StdioMcpSession("Tiny", tuple(server), timeout=15.0)
    pool._sessions["Tiny"] = session

    assert session.call_tool("ping", {}, 15.0)["content"][0]["text"] == "pong"


def test_a_tool_really_runs(server: list[str], pool: Any) -> None:
    session = StdioMcpSession("Tiny", tuple(server), timeout=15.0)
    pool._sessions["Tiny"] = session

    result = session.call_tool("echo", {"message": "hello"}, 15.0)

    assert flatten_content(result) == {"result": "hello"}


def test_structured_content_wins_over_prose(server: list[str], pool: Any) -> None:
    # The next step in a workflow reads fields, not sentences.
    session = StdioMcpSession("Tiny", tuple(server), timeout=15.0)
    pool._sessions["Tiny"] = session

    result = session.call_tool("add", {"a": 2, "b": 3}, 15.0)

    assert flatten_content(result) == {"total": 5}


def test_a_missing_program_is_reported_not_raised_as_a_crash(pool: Any) -> None:
    session = StdioMcpSession(
        "Tiny", ("definitely-not-a-real-program-9zx",), timeout=5.0
    )

    with pytest.raises(McpError, match="not found on this machine"):
        session.ensure_open()


def test_the_session_is_reused_across_calls(server: list[str], pool: Any) -> None:
    # Starting a server is the expensive part. Two calls must not mean two
    # processes.
    factory = lambda: StdioMcpSession("Tiny", tuple(server), timeout=15.0)

    first = pool.session_for("Tiny", factory)
    second = pool.session_for("Tiny", factory)

    assert first is second


def test_a_discarded_session_is_replaced(server: list[str], pool: Any) -> None:
    factory = lambda: StdioMcpSession("Tiny", tuple(server), timeout=15.0)

    first = pool.session_for("Tiny", factory)
    pool.discard("Tiny")
    second = pool.session_for("Tiny", factory)

    assert first is not second
    assert second.list_tools()


# ── discovery flows into the registry ───────────────────────────────────────


def test_probing_fills_in_the_capabilities_nobody_typed(
    server: list[str], pool: Any
) -> None:
    # This is the whole point of the kind: the entry names a command and
    # nothing else, and the capabilities arrive from the server.
    endpoint = entry_for(server)

    assert endpoint.capabilities == frozenset()

    result = probe_target(endpoint, HEALTH, pool)

    assert result.reachable
    assert result.endpoint.capabilities == {"echo", "add", "ping"}


def test_a_declared_capability_list_is_an_allow_list(
    server: list[str], pool: Any
) -> None:
    endpoint = entry_for(server, capabilities=["echo"])

    result = probe_target(endpoint, HEALTH, pool)

    assert result.endpoint.capabilities == {"echo"}
    assert [tool.name for tool in result.endpoint.tool.discovered] == ["echo"]


def test_an_allow_list_matching_nothing_is_a_failure_that_says_so(
    server: list[str], pool: Any
) -> None:
    endpoint = entry_for(server, capabilities=["not_a_tool"])

    result = probe_target(endpoint, HEALTH, pool)

    assert not result.reachable
    assert "echo" in result.detail


def test_a_server_that_will_not_start_is_unreachable(pool: Any) -> None:
    endpoint = entry_for(["definitely-not-a-real-program-9zx"])

    result = probe_target(endpoint, HEALTH, pool)

    assert not result.reachable
    assert "not found on this machine" in result.detail


def test_the_directory_offers_the_discovered_tools(server: list[str]) -> None:
    endpoint = entry_for(server)
    directory = AgentDirectory(AgentRegistry({"Tiny": endpoint}), health=HEALTH)

    try:
        directory.refresh()
        record = directory.record_for("Tiny")

        assert record is not None
        assert record.status == STATUS_READY
        assert record.endpoint.capabilities == {"echo", "add", "ping"}
        # And the planner's view agrees, which is what routing checks against.
        assert directory.snapshot().resolve("Tiny").accepts("echo")
    finally:
        directory.stop()


def test_a_dead_server_is_withdrawn(tmp_path: Path) -> None:
    endpoint = entry_for(["definitely-not-a-real-program-9zx"])
    directory = AgentDirectory(
        AgentRegistry({"Tiny": endpoint}),
        health=replace(HEALTH, failures_before_unreachable=1),
    )

    try:
        directory.refresh()
        record = directory.record_for("Tiny")

        assert record is not None and record.status == STATUS_UNREACHABLE
        assert directory.snapshot().resolve("Tiny") is None
    finally:
        directory.stop()


# ── the free-text to arguments gap ──────────────────────────────────────────


ECHO = McpTool(
    "echo",
    "Repeat a message.",
    {
        "type": "object",
        "properties": {"message": {"type": "string"}},
        "required": ["message"],
    },
)
ADD = McpTool(
    "add",
    "Add two numbers.",
    {
        "type": "object",
        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        "required": ["a", "b"],
    },
)
PING = McpTool("ping", "Answer pong.", {"type": "object", "properties": {}})


def test_the_planner_arguments_are_passed_through() -> None:
    assert arguments_for(message("add", arguments={"a": 1, "b": 2}), ADD) == {
        "a": 1,
        "b": 2,
    }


def test_a_sole_string_argument_takes_the_task() -> None:
    # The shortcut that makes most MCP tools work without the planner having
    # to think about schemas at all.
    assert arguments_for(message("echo", task="hello"), ECHO) == {"message": "hello"}


def test_a_tool_needing_nothing_is_called_with_nothing() -> None:
    assert arguments_for(message("ping"), PING) == {}


def test_a_multi_argument_tool_refuses_to_be_guessed_at() -> None:
    # Inventing `a` and `b` from a sentence is worse than a step that says
    # what it needed.
    with pytest.raises(AgentTransportError, match="needs arguments"):
        arguments_for(message("add"), ADD)


def test_the_refusal_names_what_the_tool_wanted() -> None:
    with pytest.raises(AgentTransportError) as raised:
        arguments_for(message("add"), ADD)

    assert "a (integer, required)" in str(raised.value)
    assert "b (integer, required)" in str(raised.value)


def test_an_unprobed_target_refuses_rather_than_guessing() -> None:
    with pytest.raises(AgentTransportError, match="has not been asked"):
        arguments_for(message("echo"), None)


def test_arguments_may_reference_the_message() -> None:
    # The same placeholders every other configured value understands.
    resolved = arguments_for(
        message(
            "echo",
            arguments={"message": "reviewing {data.repository.path}"},
            repository={"path": "/work/app"},
        ),
        ECHO,
    )

    assert resolved == {"message": "reviewing /work/app"}


# ── dispatching a step ──────────────────────────────────────────────────────


def dispatched(endpoint: Any, pool: Any, capability: str, **kwargs: Any) -> Any:
    """Probe a target, then run one step against it."""
    endpoint = probe_target(endpoint, HEALTH, pool).endpoint

    return McpToolTransport(pool).send(message(capability, **kwargs), endpoint, 15.0)


def test_a_step_runs_as_a_tool_call(server: list[str], pool: Any) -> None:
    reply = dispatched(entry_for(server), pool, "echo", task="say this")

    assert reply["status"] == "ok"
    assert reply["output"] == {"result": "say this"}


def test_a_step_with_arguments_runs_as_a_tool_call(
    server: list[str], pool: Any
) -> None:
    reply = dispatched(entry_for(server), pool, "add", arguments={"a": 20, "b": 22})

    assert reply["status"] == "ok"
    assert reply["output"] == {"total": 42}


def test_a_tool_reporting_failure_is_a_failed_step_not_a_crash(
    server: list[str], pool: Any
) -> None:
    # `isError` means the tool ran and said no, which is a result.
    endpoint = probe_target(entry_for(server), HEALTH, pool).endpoint
    endpoint = replace(
        endpoint,
        capabilities=endpoint.capabilities | {"explode"},
        tool=replace(
            endpoint.tool,
            discovered=endpoint.tool.discovered
            + (McpTool("explode", "", {"type": "object", "properties": {}}),),
        ),
    )

    reply = McpToolTransport(pool).send(message("explode"), endpoint, 15.0)

    assert reply["status"] == "error"
    assert "it broke" in reply["error"]


def test_asking_for_a_tool_the_server_does_not_have_says_what_it_does_have(
    server: list[str], pool: Any
) -> None:
    endpoint = probe_target(entry_for(server), HEALTH, pool).endpoint

    with pytest.raises(AgentTransportError) as raised:
        McpToolTransport(pool).send(message("nonexistent"), endpoint, 15.0)

    assert "It offers: add, echo, ping" in str(raised.value)


# ── the entry itself ────────────────────────────────────────────────────────


def test_an_entry_needs_a_command_or_a_url() -> None:
    with pytest.raises(ValueError, match="needs either a command or a url"):
        load_agent_entry("X", {"type": "mcp"})


def test_an_entry_may_not_have_both() -> None:
    with pytest.raises(ValueError, match="names both a command and a url"):
        load_agent_entry(
            "X", {"type": "mcp", "command": ["a"], "url": "https://x.example.com"}
        )


def test_a_url_entry_is_an_http_session() -> None:
    endpoint = load_agent_entry(
        "X",
        {
            "type": "mcp",
            "url": "https://mcp.example.com/mcp",
            "headers": {"Authorization": "Bearer ${MCP_TOKEN}"},
        },
    )

    assert isinstance(endpoint.tool, McpToolSpec)
    assert endpoint.tool.transport == "http"
    assert endpoint.tool.headers["Authorization"] == "Bearer ${MCP_TOKEN}"


def test_a_command_entry_is_a_stdio_session() -> None:
    endpoint = entry_for(["npx", "-y", "@modelcontextprotocol/server-filesystem"])

    assert isinstance(endpoint.tool, McpToolSpec)
    assert endpoint.tool.transport == "stdio"


def test_a_missing_token_is_reported_against_the_target(
    monkeypatch: pytest.MonkeyPatch, pool: Any
) -> None:
    # Calling an authenticated server with an empty credential and reading the
    # rejection as "the tool failed" is the confusing version of this.
    monkeypatch.delenv("KIO1_TEST_MCP_TOKEN", raising=False)
    endpoint = load_agent_entry(
        "X",
        {
            "type": "mcp",
            "url": "https://mcp.example.com/mcp",
            "headers": {"Authorization": "Bearer ${KIO1_TEST_MCP_TOKEN}"},
        },
    )

    result = probe_target(endpoint, HEALTH, pool)

    assert not result.reachable
    assert "KIO1_TEST_MCP_TOKEN" in result.detail


# ── what the catalog shows the planner ──────────────────────────────────────


def test_the_catalog_shows_each_tool_and_its_arguments(server: list[str]) -> None:
    from core.prompt_loader import render_catalog

    endpoint = probe_target(entry_for(server), HEALTH, None).endpoint
    catalog = render_catalog(AgentRegistry({"Tiny": endpoint}))

    assert "_(MCP server)_" in catalog
    assert "`add`" in catalog
    assert "arguments: a (integer, required), b (integer, required)" in catalog


def test_an_unprobed_server_advertises_nothing_specific() -> None:
    from core.prompt_loader import render_catalog

    catalog = render_catalog(AgentRegistry({"Tiny": entry_for(["npx", "x"])}))

    assert "`echo`" not in catalog


# ── the plan schema ─────────────────────────────────────────────────────────


def test_a_plan_may_carry_arguments() -> None:
    from dispatch.workflow_planner import parse_plan

    plan = parse_plan(
        {
            "workflow_id": "wf-1",
            "execution_mode": "sequential",
            "steps": [
                {
                    "step_id": "s1",
                    "agent_id": "Tiny",
                    "capability": "add",
                    "task": "add them",
                    "arguments": {"a": 1, "b": 2},
                }
            ],
        }
    )

    assert plan.steps[0].arguments == {"a": 1, "b": 2}


def test_malformed_arguments_lose_the_arguments_not_the_plan() -> None:
    from dispatch.workflow_planner import parse_plan

    plan = parse_plan(
        {
            "workflow_id": "wf-1",
            "execution_mode": "sequential",
            "steps": [
                {
                    "step_id": "s1",
                    "agent_id": "Tiny",
                    "capability": "add",
                    "task": "add them",
                    "arguments": "a=1, b=2",
                }
            ],
        }
    )

    assert plan.steps[0].arguments == {}
    assert plan.steps[0].task == "add them"


def test_arguments_reach_the_execution_message() -> None:
    from dispatch.workflow_planner import PlanStep, WorkflowPlan, route_plan

    plan = WorkflowPlan(
        workflow_id="wf-1",
        execution_mode="sequential",
        steps=(PlanStep("s1", "Tiny", "add", "add them", arguments={"a": 1, "b": 2}),),
    )
    registry = AgentRegistry({"Tiny": entry_for(["npx", "x"])})

    routed = route_plan(plan, registry)

    assert routed.messages[0].data["arguments"] == {"a": 1, "b": 2}


def test_a_step_without_arguments_carries_none() -> None:
    from dispatch.workflow_planner import PlanStep, WorkflowPlan, route_plan

    plan = WorkflowPlan(
        workflow_id="wf-1",
        execution_mode="sequential",
        steps=(PlanStep("s1", "Tiny", "echo", "say it"),),
    )
    registry = AgentRegistry({"Tiny": entry_for(["npx", "x"])})

    assert "arguments" not in route_plan(plan, registry).messages[0].data


# ── what comes back ─────────────────────────────────────────────────────────


def test_text_blocks_are_joined() -> None:
    assert flatten_content(
        {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}
    ) == {"result": "a\nb"}


def test_a_non_text_block_is_named_rather_than_dropped() -> None:
    # An image coming back must not look like an empty answer.
    output = flatten_content(
        {"content": [{"type": "text", "text": "here"}, {"type": "image"}]}
    )

    assert output == {"result": "here", "attachments": ["image"]}


def test_an_empty_result_is_still_an_object() -> None:
    assert flatten_content({}) == {"result": ""}


def test_a_long_result_is_bounded() -> None:
    long = "x" * 20000
    output = flatten_content({"content": [{"type": "text", "text": long}]})

    assert len(output["result"]) < len(long)
    assert "truncated" in output["result"]
