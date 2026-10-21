"""Tests for switching a target off without removing it.

The property that matters: a disabled target is kept — definition, place in
the console, ability to come back with one click — and is otherwise
invisible. It is not planned for, not routed to, and not contacted.
"""

from typing import Any

import pytest

from core.prompt_loader import render_catalog
from dispatch.agent_registry import load_agent_entry, load_agent_registry
from dispatch.directory import (
    STATUS_UNCHECKED,
    AgentDirectory,
    HealthConfig,
)

HEALTH = HealthConfig(enabled=True, interval=0.1, timeout=1)

# Nothing listens on port 1, so any attempt to probe this fails.
NOWHERE = "http://127.0.0.1:1"


def entry(**extra: Any) -> dict[str, Any]:
    """Return a KIO entry at an address nothing answers on."""
    return {"endpoint": NOWHERE, "capabilities": ["diagnosis"], **extra}


def test_a_target_is_enabled_unless_it_says_otherwise() -> None:
    assert load_agent_entry("KIO2", entry()).enabled is True


@pytest.mark.parametrize(
    "definition",
    [
        entry(enabled=False),
        {"type": "cli", "command": ["git", "log"], "enabled": False},
        {"type": "http", "url": "http://127.0.0.1:1/x", "enabled": False},
        {"type": "mcp", "command": ["server"], "enabled": False},
    ],
    ids=["kio", "cli", "http", "mcp"],
)
def test_every_kind_can_be_disabled(definition: dict[str, Any]) -> None:
    assert load_agent_entry("Target", definition).enabled is False


def test_enabled_must_be_a_boolean() -> None:
    # "false" as a string is truthy, and reading it as on would be exactly
    # the wrong way round.
    with pytest.raises(ValueError, match="enabled must be true or false"):
        load_agent_entry("KIO2", entry(enabled="false"))


def test_a_disabled_target_is_kept_but_not_offered() -> None:
    directory = AgentDirectory(
        load_agent_registry(
            {
                "KIO2": entry(enabled=False),
                "KIO3": {"endpoint": NOWHERE, "capabilities": ["requirements"]},
            }
        )
    )

    assert [record.agent_id for record in directory.records()] == ["KIO2", "KIO3"]
    assert directory.snapshot().agent_ids() == frozenset({"KIO3"})
    assert directory.record_for("KIO2").to_summary()["enabled"] is False


def test_a_disabled_target_is_left_out_of_the_catalog() -> None:
    directory = AgentDirectory(
        load_agent_registry({"KIO2": entry(enabled=False), "KIO3": entry()})
    )

    catalog = render_catalog(directory.snapshot())

    assert "KIO3" in catalog
    assert "KIO2" not in catalog


def test_registering_a_disabled_target_does_not_contact_it() -> None:
    # Probing NOWHERE would mark it unreachable; staying unchecked proves no
    # probe was made — which for an MCP server means it was not started.
    directory = AgentDirectory(load_agent_registry({}), health=HEALTH)

    record = directory.register("KIO2", entry(enabled=False))

    assert record.status == STATUS_UNCHECKED
    assert record.last_checked == 0.0


def test_background_probing_skips_a_disabled_target() -> None:
    directory = AgentDirectory(
        load_agent_registry({"KIO2": entry(enabled=False)}), health=HEALTH
    )

    directory.refresh()

    assert directory.record_for("KIO2").status == STATUS_UNCHECKED


def test_switching_a_target_back_on_probes_it_again() -> None:
    directory = AgentDirectory(load_agent_registry({"KIO2": entry(enabled=False)}))

    record = directory.replace_entry("KIO2", entry(enabled=True))

    # Back on, it is checked like any edit — and NOWHERE does not answer.
    assert record.endpoint.enabled is True
    assert record.last_checked > 0
    assert not record.usable
