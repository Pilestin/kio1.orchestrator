"""The stand-in fleet used to see a whole plan execute."""

from typing import Any

import pytest

from core.config_loader import load_agent_registry
from tools.dummy_agent import _CAPABILITY_OUTPUTS
from tools.dummy_fleet import FLEET, port_of, registry_entry


def test_every_kio_from_the_prompt_has_a_stub() -> None:
    # A missing agent means a plan step is reported as skipped, which looks
    # like a configuration mistake rather than a deliberate gap.
    assert set(FLEET) == {f"KIO{n}" for n in range(2, 14)}


def test_ports_follow_the_registry_convention() -> None:
    assert port_of("KIO2") == 8102
    assert port_of("KIO9") == 8109
    assert port_of("KIO13") == 8113


def test_every_port_is_distinct() -> None:
    ports = [port_of(agent_id) for agent_id in FLEET]

    assert len(set(ports)) == len(ports)


def test_an_identifier_without_a_number_is_rejected() -> None:
    with pytest.raises(ValueError, match="Cannot derive a port"):
        port_of("KIOX")


def test_every_declared_capability_has_a_canned_answer() -> None:
    # A capability with no output would answer with a generic acknowledgement,
    # which teaches nothing about what that KIO is for.
    declared = {
        capability for capabilities in FLEET.values() for capability in capabilities
    }
    # Replay and alignment answer from a recording rather than a canned body.
    without_output = declared - set(_CAPABILITY_OUTPUTS) - {"replay", "trace_alignment"}

    assert without_output == set()


def test_a_generated_entry_loads_as_configuration() -> None:
    entries: dict[str, Any] = {
        agent_id: registry_entry(agent_id, "127.0.0.1") for agent_id in FLEET
    }

    registry = load_agent_registry(entries)

    assert registry.agent_ids() == frozenset(FLEET)
    kio7 = registry.resolve("KIO7")
    assert kio7 is not None
    assert kio7.url == "http://127.0.0.1:8107/execute"
    assert "code_generation" in kio7.capabilities


def test_canned_answers_declare_a_verdict_kio1_understands() -> None:
    allowed = {"defect", "clean", "inconclusive"}

    for capability, output in _CAPABILITY_OUTPUTS.items():
        verdict = output.get("verdict")

        assert (
            verdict is None or verdict in allowed
        ), f"{capability} declares an unusable verdict: {verdict!r}"
