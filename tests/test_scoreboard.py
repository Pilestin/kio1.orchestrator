"""Tests for choosing between targets by what they have actually done.

Failover decides who *could* carry out a step. These check the order they are
asked in, and that the order is earned rather than assumed.
"""

import json
from typing import Any

import pytest

from core.config_loader import DispatchConfig, load_config
from dispatch.agent_registry import AgentEndpoint, load_agent_registry
from dispatch.failover import FailoverConfig, candidates_for
from dispatch.scoreboard import (
    UNPROVEN_RATE,
    Scoreboard,
    SelectionConfig,
    load_selection_config,
)
from dispatch.workflow_executor import STATUS_OK, dispatch_plan, execute_workflow
from dispatch.workflow_planner import ExecutionMessage, parse_plan

ON = SelectionConfig(enabled=True, window=20, explore_every=0)
OFF = SelectionConfig()
FAILOVER = FailoverConfig(enabled=True, max_attempts=3)

REGISTRY = load_agent_registry(
    {
        "KIO7": {
            "endpoint": "http://127.0.0.1:8107",
            "capabilities": ["code_generation"],
        },
        "Beta": {
            "endpoint": "http://127.0.0.1:8207",
            "capabilities": ["code_generation"],
        },
        "Gamma": {
            "endpoint": "http://127.0.0.1:8307",
            "capabilities": ["code_generation"],
        },
    }
)


def order(scoreboard: Scoreboard, planned: str = "KIO7") -> list[str]:
    """Return the ids of the candidates, in the order they would be tried."""
    return [
        entry.agent_id
        for entry in candidates_for(
            planned, "code_generation", REGISTRY, FAILOVER, scoreboard
        )
    ]


def feed(
    scoreboard: Scoreboard, agent_id: str, results: str, duration_ms: int = 10
) -> None:
    """Record a run of outcomes, written as a string of 'o' and 'x'."""
    for mark in results:
        scoreboard.record(
            agent_id, "code_generation", ok=mark == "o", duration_ms=duration_ms
        )


# ── what a score says ────────────────────────────────────────────────────────


def test_a_target_with_no_history_has_no_score() -> None:
    assert Scoreboard(ON).score_for("KIO7", "code_generation") is None


def test_the_rate_is_smoothed_so_one_success_is_not_a_perfect_record() -> None:
    board = Scoreboard(ON)
    feed(board, "KIO7", "o")
    feed(board, "Beta", "o" * 19 + "x")

    lucky = board.score_for("KIO7", "code_generation")
    proven = board.score_for("Beta", "code_generation")

    assert lucky is not None and proven is not None
    assert lucky.rate == pytest.approx(2 / 3)
    assert proven.rate == pytest.approx(20 / 22)
    assert proven.rate > lucky.rate


def test_capabilities_are_scored_separately() -> None:
    # A target can be reliable at one thing and not another.
    board = Scoreboard(ON)
    board.record("KIO2", "diagnosis", ok=True, duration_ms=10)
    board.record("KIO2", "replay", ok=False, duration_ms=10)

    good = board.score_for("KIO2", "diagnosis")
    bad = board.score_for("KIO2", "replay")

    assert good is not None and bad is not None
    assert good.rate > bad.rate


def test_only_successful_attempts_count_towards_the_mean_duration() -> None:
    board = Scoreboard(ON)
    board.record("KIO7", "code_generation", ok=True, duration_ms=100)
    board.record("KIO7", "code_generation", ok=False, duration_ms=9000)

    score = board.score_for("KIO7", "code_generation")

    assert score is not None
    assert score.mean_success_ms == 100


def test_the_window_forgets_what_a_target_used_to_be() -> None:
    board = Scoreboard(SelectionConfig(enabled=True, window=4, explore_every=0))
    feed(board, "KIO7", "xxxx")
    feed(board, "KIO7", "oooo")

    score = board.score_for("KIO7", "code_generation")

    assert score is not None
    assert score.attempts == 4
    assert score.successes == 4


# ── the order that comes out ─────────────────────────────────────────────────


def test_with_no_history_the_planned_agent_still_goes_first() -> None:
    assert order(Scoreboard(ON)) == ["KIO7", "Beta", "Gamma"]


def test_a_failing_planned_agent_is_overtaken() -> None:
    board = Scoreboard(ON)
    feed(board, "KIO7", "xxxx")

    assert order(board)[0] != "KIO7"


def test_a_working_target_overtakes_an_untried_one() -> None:
    board = Scoreboard(ON)
    feed(board, "Gamma", "ooooo")

    assert order(board, planned="Beta")[0] == "Gamma"


def test_an_untried_target_outranks_one_that_keeps_failing() -> None:
    # This is what stops a newly registered agent from being locked out: it
    # sits at the unproven rate, which is above a failing record.
    board = Scoreboard(ON)
    feed(board, "KIO7", "xxx")
    feed(board, "Beta", "ooo")

    assert order(board) == ["Beta", "Gamma", "KIO7"]


def test_the_faster_of_two_equally_reliable_targets_goes_first() -> None:
    board = Scoreboard(ON)
    feed(board, "KIO7", "ooo", duration_ms=900)
    feed(board, "Beta", "ooo", duration_ms=30)

    assert order(board)[0] == "Beta"


def test_a_genuine_tie_never_moves_the_step() -> None:
    board = Scoreboard(ON)
    feed(board, "KIO7", "ooo", duration_ms=50)
    feed(board, "Beta", "ooo", duration_ms=50)

    assert order(board)[0] == "KIO7"


def test_an_unproven_target_sits_where_the_formula_puts_it() -> None:
    board = Scoreboard(ON)
    feed(board, "KIO7", "o")

    score = board.score_for("KIO7", "code_generation")

    assert score is not None
    assert score.rate > UNPROVEN_RATE


def test_selection_off_leaves_the_order_alone() -> None:
    board = Scoreboard(OFF)
    feed(board, "KIO7", "xxxxx")

    assert order(board) == ["KIO7", "Beta", "Gamma"]


def test_history_decides_which_candidates_are_worth_the_attempts() -> None:
    # max_attempts caps the list *after* ranking, so the two best go through
    # rather than the two the configuration happened to list first.
    board = Scoreboard(ON)
    feed(board, "KIO7", "xxx")
    feed(board, "Gamma", "ooo")

    capped = [
        entry.agent_id
        for entry in candidates_for(
            "KIO7",
            "code_generation",
            REGISTRY,
            FailoverConfig(enabled=True, max_attempts=2),
            board,
        )
    ]

    assert capped == ["Gamma", "Beta"]


# ── history is written by running workflows ──────────────────────────────────


class ScriptedTransport:
    """Answers each agent the way the test told it to."""

    def __init__(self, replies: dict[str, dict[str, Any]]) -> None:
        self.replies = replies
        self.called: list[str] = []

    def send(
        self, message: ExecutionMessage, agent: AgentEndpoint, timeout: float
    ) -> dict[str, Any]:
        self.called.append(agent.agent_id)
        reply = self.replies.get(agent.agent_id)

        if reply is None:
            from dispatch.agent_transport import AgentTransportError

            raise AgentTransportError(f"{agent.agent_id} is unreachable")

        return {
            "workflow_id": message.workflow_id,
            "step_id": message.step_id,
            **reply,
        }

    def close(self) -> None:
        """Nothing to release."""


OK = {"status": "ok", "output": {"summary": "done"}}


def plan_data() -> dict[str, Any]:
    """Return a one-step plan asking KIO7 for code generation."""
    return {
        "workflow_id": "wf-sb01",
        "execution_mode": "sequential",
        "steps": [
            {
                "step_id": "s1",
                "agent_id": "KIO7",
                "capability": "code_generation",
                "task": "Write it",
            }
        ],
        "explanation": "One step.",
    }


def run_once(board: Scoreboard, replies: dict[str, dict[str, Any]]) -> list[str]:
    """Execute the plan once and return who was actually asked."""
    transport = ScriptedTransport(replies)
    execute_workflow(
        parse_plan(plan_data()),
        REGISTRY,
        transport,
        timeout=5,
        max_parallel_steps=2,
        failover=FAILOVER,
        scoreboard=board,
    )

    return transport.called


def test_a_run_records_what_every_target_did() -> None:
    board = Scoreboard(ON)
    run_once(board, {"Beta": OK})

    failed = board.score_for("KIO7", "code_generation")
    worked = board.score_for("Beta", "code_generation")

    assert failed is not None and failed.successes == 0
    assert worked is not None and worked.successes == 1


def test_the_orchestrator_stops_asking_a_target_that_keeps_failing() -> None:
    # The point of the whole feature, end to end: three runs, and by the
    # third the broken agent is no longer the first one asked.
    board = Scoreboard(ON)

    first = run_once(board, {"Beta": OK})
    run_once(board, {"Beta": OK})
    third = run_once(board, {"Beta": OK})

    assert first == ["KIO7", "Beta"]
    assert third == ["Beta"]


def test_without_selection_it_keeps_asking_the_failing_target_first() -> None:
    board = Scoreboard(OFF)

    run_once(board, {"Beta": OK})
    run_once(board, {"Beta": OK})

    assert run_once(board, {"Beta": OK}) == ["KIO7", "Beta"]


def test_a_recovered_target_earns_its_place_back() -> None:
    board = Scoreboard(ON)
    feed(board, "KIO7", "xxx")

    assert order(board)[0] != "KIO7"

    feed(board, "KIO7", "oooooooo")

    assert order(board)[0] == "KIO7"


def test_the_scoreboard_reaches_the_executor_through_dispatch_plan() -> None:
    board = Scoreboard(ON)
    result = dispatch_plan(
        plan_data(),
        DispatchConfig(
            enabled=True,
            registry=REGISTRY,
            failover=FAILOVER,
            selection=ON,
        ),
        ScriptedTransport({"KIO7": OK}),
        board,
    )

    assert result is not None
    assert result.results[0].status == STATUS_OK
    assert board.score_for("KIO7", "code_generation") is not None


# ── reporting ────────────────────────────────────────────────────────────────


def test_every_score_for_one_target_can_be_read_back() -> None:
    board = Scoreboard(ON)
    board.record("KIO2", "diagnosis", ok=True, duration_ms=10)
    board.record("KIO2", "replay", ok=True, duration_ms=20)
    board.record("KIO7", "code_generation", ok=True, duration_ms=30)

    scores = board.scores_for_agent("KIO2")

    assert [score.capability for score in scores] == ["diagnosis", "replay"]


def test_a_score_summary_is_json_safe() -> None:
    board = Scoreboard(ON)
    feed(board, "KIO7", "oox", duration_ms=40)

    summary = board.summary()[0].to_summary()

    assert json.dumps(summary)
    assert summary["attempts"] == 3
    assert summary["successes"] == 2
    assert summary["mean_success_ms"] == 40


# ── configuration ────────────────────────────────────────────────────────────


def test_selection_is_off_unless_configured() -> None:
    assert load_selection_config(None).enabled is False


def test_selection_settings_are_read() -> None:
    selection = load_selection_config({"enabled": True, "window": 50})

    assert selection.enabled is True
    assert selection.window == 50


def test_a_zero_window_is_rejected() -> None:
    with pytest.raises(ValueError, match="must be a positive integer"):
        load_selection_config({"window": 0})


def test_the_repository_config_turns_selection_on() -> None:
    selection = load_config("config.json").dispatch.selection

    assert selection.enabled is True


def test_a_malformed_selection_block_is_rejected_when_the_config_loads(
    tmp_path: Any,
) -> None:
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
                "request_timeout": 60,
                "keep_alive": -1,
                "max_output_tokens": 1024,
                "dispatch": {"selection": {"window": "lots"}},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="window must be a positive integer"):
        load_config(str(config_path))


# ── finding out about a target nobody has asked ──────────────────────────────


def test_a_proven_target_would_otherwise_lock_out_a_better_one() -> None:
    # One success is enough to outrank everything untried, so without
    # exploration a newly registered agent is never asked and never earns a
    # record. This is the behaviour exploration exists to break.
    board = Scoreboard(ON)
    feed(board, "Beta", "o")

    assert order(board, planned="Beta")[0] == "Beta"
    assert order(board, planned="Beta")[0] == "Beta"


def test_every_nth_decision_tries_something_new() -> None:
    board = Scoreboard(SelectionConfig(enabled=True, window=20, explore_every=3))
    feed(board, "KIO7", "ooo")
    feed(board, "Beta", "ooo")

    first = [order(board)[0] for _ in range(6)]

    # Gamma has never been asked; it gets a turn on decisions 3 and 6.
    assert first == ["KIO7", "KIO7", "Gamma", "KIO7", "KIO7", "Gamma"]


def test_exploring_does_nothing_when_the_least_sampled_already_leads() -> None:
    board = Scoreboard(SelectionConfig(enabled=True, window=20, explore_every=2))
    feed(board, "KIO7", "oo")
    feed(board, "Beta", "oooo")
    feed(board, "Gamma", "oooo")

    # KIO7 has the fewest samples and is already first, so there is nothing
    # to learn by reordering.
    assert [order(board)[0] for _ in range(4)] == ["KIO7"] * 4


def test_a_target_tried_once_keeps_getting_turns_until_it_can_compete() -> None:
    # One lucky success is not enough evidence to overtake an incumbent, so
    # exploring only untried candidates would leave it stuck forever.
    board = Scoreboard(SelectionConfig(enabled=True, window=20, explore_every=2))
    feed(board, "KIO7", "o" * 10, duration_ms=900)
    feed(board, "Gamma", "o", duration_ms=10)
    feed(board, "Beta", "o" * 10, duration_ms=900)

    assert order(board)[0] == "KIO7"
    assert order(board)[0] == "Gamma"


def test_exploration_can_be_turned_off() -> None:
    board = Scoreboard(SelectionConfig(enabled=True, window=20, explore_every=0))
    feed(board, "KIO7", "ooo")

    assert [order(board)[0] for _ in range(5)] == ["KIO7"] * 5


def test_explore_every_is_read_from_configuration() -> None:
    assert load_selection_config({"explore_every": 4}).explore_every == 4
    assert load_selection_config({}).explore_every == 10


def test_a_negative_explore_every_is_rejected() -> None:
    with pytest.raises(ValueError, match="zero or a positive integer"):
        load_selection_config({"explore_every": -1})


def test_speed_decides_between_two_targets_that_both_simply_work() -> None:
    # Without rounding, the one with more samples of the same perfect record
    # would win forever and the faster one could never overtake it.
    board = Scoreboard(ON)
    feed(board, "Beta", "o" * 8, duration_ms=1000)
    feed(board, "Gamma", "o" * 8, duration_ms=20)

    assert order(board, planned="Beta")[0] == "Gamma"


def test_a_clearly_worse_target_does_not_win_on_speed() -> None:
    board = Scoreboard(ON)
    feed(board, "Beta", "o" * 10, duration_ms=1000)
    feed(board, "Gamma", "xxxxxoooo", duration_ms=5)

    assert order(board, planned="Beta")[0] == "Beta"


def test_history_is_recorded_for_steps_that_run_side_by_side() -> None:
    # A level dispatches its steps concurrently, and that path has its own
    # call into the transport. Without the scoreboard reaching it, every
    # parallel and mixed workflow would leave no history behind at all.
    board = Scoreboard(ON)
    plan = {
        "workflow_id": "wf-parallel",
        "execution_mode": "mixed",
        "steps": [
            {
                "step_id": "s1",
                "agent_id": "KIO7",
                "capability": "code_generation",
                "task": "Write it",
            },
            {
                "step_id": "s2",
                "agent_id": "Beta",
                "capability": "code_generation",
                "task": "Write something else",
            },
            {
                "step_id": "s3",
                "agent_id": "Gamma",
                "capability": "code_generation",
                "task": "And this",
                "depends_on": ["s1"],
            },
        ],
        "explanation": "Two steps share a level.",
    }

    execute_workflow(
        parse_plan(plan),
        REGISTRY,
        ScriptedTransport({"KIO7": OK, "Beta": OK, "Gamma": OK}),
        timeout=5,
        max_parallel_steps=4,
        scoreboard=board,
    )

    recorded = {score.agent_id for score in board.summary()}

    assert recorded == {"KIO7", "Beta", "Gamma"}
