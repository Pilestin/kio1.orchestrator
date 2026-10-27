"""Tests for arguments that take their value from an earlier step's output.

The properties that matter: `${s1.latitude}` is filled from s1's output when
s2 is sent — exactly, by the field's name when the path was a guess, and
with its type kept; a step that refers to another runs after it even when
the plan forgot to say so; a follow-up round's renamed steps keep their
references; and a reference to nothing is left visible, never invented.
"""

from typing import Any

from dispatch.agent_registry import AgentEndpoint, load_agent_registry
from dispatch.review import prefix_plan
from dispatch.step_references import referenced_steps, rename_references, resolve
from dispatch.workflow_executor import execute_workflow
from dispatch.workflow_planner import ExecutionMessage, parse_plan

GEOCODED = {"results": [{"name": "Ankara", "latitude": 39.93, "longitude": 32.86}]}


def test_a_path_that_is_there_is_followed() -> None:
    value, unresolved = resolve("${s1.results.0.latitude}", {"s1": GEOCODED})

    assert value == 39.93
    assert unresolved == []


def test_a_guessed_path_finds_the_field_by_its_name() -> None:
    value, _ = resolve("${s1.latitude}", {"s1": GEOCODED})

    assert value == 39.93


def test_a_reference_inside_text_is_written_into_it() -> None:
    value, _ = resolve("lat=${s1.latitude}&city=${s1.name}", {"s1": GEOCODED})

    assert value == "lat=39.93&city=Ankara"


def test_a_reference_to_nothing_is_left_as_written() -> None:
    value, unresolved = resolve({"x": "${s1.altitude}"}, {"s1": GEOCODED})

    assert value == {"x": "${s1.altitude}"}
    assert unresolved == ["${s1.altitude}"]


def test_an_environment_variable_is_not_a_step() -> None:
    assert referenced_steps({"token": "${API_TOKEN}"}, {"s1", "s2"}) == []
    assert resolve("${API_TOKEN}", {"s1": GEOCODED})[0] == "${API_TOKEN}"


def test_a_step_runs_after_the_step_it_takes_a_value_from() -> None:
    plan = parse_plan(
        {
            "workflow_id": "wf-1",
            "execution_mode": "parallel",
            "steps": [
                {
                    "step_id": "s1",
                    "agent_id": "Geo",
                    "capability": "geo",
                    "task": "Find",
                },
                {
                    "step_id": "s2",
                    "agent_id": "Weather",
                    "capability": "weather",
                    "task": "Weather there",
                    "arguments": {"latitude": "${s1.latitude}"},
                },
            ],
        }
    )

    assert plan.steps[1].depends_on == ("s1",)


def test_a_follow_up_rounds_references_follow_the_renamed_steps() -> None:
    assert rename_references({"lat": "${s1.latitude}"}, {"s1": "r2-s1"}) == {
        "lat": "${r2-s1.latitude}"
    }

    plan = prefix_plan(
        parse_plan(
            {
                "workflow_id": "wf-1",
                "execution_mode": "sequential",
                "steps": [
                    {"step_id": "s1", "agent_id": "G", "capability": "g", "task": "t"},
                    {
                        "step_id": "s2",
                        "agent_id": "W",
                        "capability": "w",
                        "task": "t",
                        "arguments": {"lat": "${s1.latitude}"},
                    },
                ],
            }
        ),
        "r2-",
    )

    assert plan.steps[1].arguments == {"lat": "${r2-s1.latitude}"}
    assert plan.steps[1].depends_on == ("r2-s1",)


class Recording:
    """Answers the geocoder with coordinates and keeps what each step got."""

    def __init__(self) -> None:
        self.arguments: dict[str, Any] = {}

    def send(
        self, message: ExecutionMessage, agent: AgentEndpoint, timeout: float
    ) -> dict[str, Any]:
        self.arguments[message.step_id] = message.data.get("arguments")
        output = GEOCODED if message.agent_id == "Geo" else {"temperature": 12.3}
        return {
            "workflow_id": message.workflow_id,
            "step_id": message.step_id,
            "status": "ok",
            "output": output,
        }

    def close(self) -> None:
        """Nothing to close."""


def test_the_value_reaches_the_step_when_it_is_sent() -> None:
    registry = load_agent_registry(
        {
            "Geo": {
                "type": "http",
                "url": "https://geo.test/?q={arguments.name}",
                "capabilities": ["geo"],
            },
            "Weather": {
                "type": "http",
                "url": "https://wx.test/?lat={arguments.latitude}&lon={arguments.longitude}",
                "capabilities": ["weather"],
            },
        }
    )
    plan = parse_plan(
        {
            "workflow_id": "wf-1",
            "execution_mode": "sequential",
            "steps": [
                {
                    "step_id": "s1",
                    "agent_id": "Geo",
                    "capability": "geo",
                    "task": "Find Ankara",
                    "arguments": {"name": "Ankara"},
                },
                {
                    "step_id": "s2",
                    "agent_id": "Weather",
                    "capability": "weather",
                    "task": "Weather there",
                    "arguments": {
                        "latitude": "${s1.latitude}",
                        "longitude": "${s1.longitude}",
                    },
                },
            ],
        }
    )
    transport = Recording()

    result = execute_workflow(
        plan, registry, transport, timeout=5, max_parallel_steps=2  # type: ignore[arg-type]
    )

    assert result.succeeded == 2
    assert transport.arguments["s2"] == {"latitude": 39.93, "longitude": 32.86}
