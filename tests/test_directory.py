"""Tests for the live directory: agents joining, going away, and coming back.

The property that matters is that the planner is never offered a target that
cannot be reached. Everything here checks some path to that.
"""

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient

from app import REGISTRY_TOKEN_ENV, _State, app, get_state
from core.prompt_loader import render_catalog
from dispatch import directory as directory_module
from dispatch.agent_registry import AgentRegistry, load_agent_registry
from dispatch.directory import (
    ORIGIN_CONFIG,
    ORIGIN_DISCOVERY,
    ORIGIN_REGISTRATION,
    STATUS_READY,
    STATUS_UNCHECKED,
    STATUS_UNREACHABLE,
    AgentDirectory,
    HealthConfig,
    load_health_config,
    probe_target,
)
from dispatch.discovery import DiscoveryConfig, ManifestSource
from dispatch.scoreboard import Scoreboard


def write_agents(path: Path, agents: dict[str, Any]) -> None:
    """Put a `dispatch.agents` block into a configuration file."""
    document = json.loads(path.read_text(encoding="utf-8"))
    document.setdefault("dispatch", {})["agents"] = agents
    path.write_text(json.dumps(document, indent=4), encoding="utf-8")


def read_saved(path: Path) -> dict[str, Any]:
    """Return the `dispatch.agents` a configuration file now holds."""
    return json.loads(path.read_text(encoding="utf-8"))["dispatch"]["agents"]


HEALTH = HealthConfig(enabled=True, interval=0.1, timeout=1)


class AgentHandler(BaseHTTPRequestHandler):
    """A stub agent whose liveness and manifest the test controls."""

    alive: ClassVar[bool] = True
    # Health and the manifest are switched independently, because an agent
    # whose manifest still answers while its health check fails is exactly the
    # case where a refresh could wipe what probing learned.
    healthy: ClassVar[bool] = True
    capabilities: ClassVar[list[str]] = ["diagnosis"]
    agent_id: ClassVar[str] = "KIO2"

    def do_GET(self) -> None:
        path = self.path.rstrip("/")

        if not type(self).alive:
            self._send(503, {"status": "down"})
            return

        if path == "/health":
            if not type(self).healthy:
                self._send(503, {"status": "unhealthy"})
                return

            self._send(200, {"status": "ok"})
            return

        if path == "/manifest":
            self._send(
                200,
                {
                    "agent_id": type(self).agent_id,
                    "description": "A stub that can be switched off.",
                    "capabilities": type(self).capabilities,
                },
            )
            return

        self._send(404, {"error": "unknown"})

    def _send(self, code: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: Any) -> None:
        """Keep the test output clean."""


@pytest.fixture
def agent() -> Iterator[str]:
    """Run a stub agent and yield its origin."""
    AgentHandler.alive = True
    AgentHandler.healthy = True
    AgentHandler.capabilities = ["diagnosis"]
    AgentHandler.agent_id = "KIO2"

    server = ThreadingHTTPServer(("127.0.0.1", 0), AgentHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    try:
        yield f"http://127.0.0.1:{int(server.server_address[1])}"
    finally:
        server.shutdown()
        server.server_close()


def kio_registry(origin: str, agent_id: str = "KIO2") -> AgentRegistry:
    """Return a registry holding one KIO agent at an address."""
    return load_agent_registry(
        {agent_id: {"endpoint": origin, "capabilities": ["diagnosis"]}}
    )


# ── probing ──────────────────────────────────────────────────────────────────


def test_a_live_agent_probes_reachable(agent: str) -> None:
    endpoint = kio_registry(agent).entries()[0]

    outcome = probe_target(endpoint, HEALTH)

    assert (outcome.reachable, outcome.detail) == (True, "")


def test_an_agent_that_is_down_probes_unreachable(agent: str) -> None:
    AgentHandler.alive = False
    endpoint = kio_registry(agent).entries()[0]
    outcome = probe_target(endpoint, HEALTH)
    reachable, detail = outcome.reachable, outcome.detail

    assert reachable is False
    assert "503" in detail


def test_a_local_command_is_probed_by_looking_for_the_program() -> None:
    present = load_agent_registry(
        {"Git": {"type": "cli", "command": ["git", "log"], "capabilities": ["h"]}}
    ).entries()[0]
    missing = load_agent_registry(
        {"Nope": {"type": "cli", "command": ["kio1-not-a-program"], "capabilities": []}}
    ).entries()[0]

    assert (probe_target(present, HEALTH).reachable) is True
    assert probe_target(missing, HEALTH).reachable is False


def test_an_api_with_no_health_url_is_not_guessed_at() -> None:
    # An arbitrary REST API has no path KIO1 may assume, and inventing one
    # would withdraw a working tool for answering 404.
    endpoint = load_agent_registry(
        {"Api": {"type": "http", "url": "http://127.0.0.1:1/run", "capabilities": []}}
    ).entries()[0]

    outcome = probe_target(endpoint, HEALTH)

    assert (outcome.reachable, outcome.detail) == (True, "not probeable")


# ── going away and coming back ───────────────────────────────────────────────


def test_an_agent_that_goes_away_stops_being_offered(agent: str) -> None:
    directory = AgentDirectory(kio_registry(agent), health=HEALTH)
    directory.refresh()

    assert directory.snapshot().agent_ids() == {"KIO2"}

    AgentHandler.alive = False

    for _ in range(HEALTH.failures_before_unreachable):
        directory.refresh()

    record = directory.record_for("KIO2")

    assert record is not None
    assert record.status == STATUS_UNREACHABLE
    assert directory.snapshot().agent_ids() == frozenset()


def test_one_blip_does_not_withdraw_an_agent(agent: str) -> None:
    directory = AgentDirectory(kio_registry(agent), health=HEALTH)
    directory.refresh()

    AgentHandler.alive = False
    directory.refresh()

    record = directory.record_for("KIO2")

    assert record is not None
    assert record.failures == 1
    assert record.status == STATUS_READY
    assert "KIO2" in directory.snapshot().agent_ids()


def test_an_agent_that_comes_back_is_offered_again(agent: str) -> None:
    directory = AgentDirectory(kio_registry(agent), health=HEALTH)
    AgentHandler.alive = False

    for _ in range(HEALTH.failures_before_unreachable):
        directory.refresh()

    assert directory.snapshot().agent_ids() == frozenset()

    AgentHandler.alive = True
    directory.refresh()
    record = directory.record_for("KIO2")

    assert record is not None
    assert record.status == STATUS_READY
    assert record.failures == 0
    assert "KIO2" in directory.snapshot().agent_ids()


def test_a_withdrawn_agent_disappears_from_the_catalog(agent: str) -> None:
    # The whole point: the model stops being told about something it cannot
    # reach, rather than planning a step that fails at dispatch.
    directory = AgentDirectory(kio_registry(agent), health=HEALTH)
    directory.refresh()

    assert "KIO2" in render_catalog(directory.snapshot())

    AgentHandler.alive = False
    for _ in range(HEALTH.failures_before_unreachable):
        directory.refresh()

    assert "KIO2" not in render_catalog(directory.snapshot())


def test_probing_is_off_unless_it_is_turned_on(agent: str) -> None:
    AgentHandler.alive = False
    directory = AgentDirectory(kio_registry(agent))
    directory.refresh()

    record = directory.record_for("KIO2")

    assert record is not None
    assert record.status == STATUS_UNCHECKED
    assert "KIO2" in directory.snapshot().agent_ids()


# ── joining while KIO1 runs ──────────────────────────────────────────────────


def test_an_agent_that_starts_later_joins_on_a_refresh(agent: str) -> None:
    directory = AgentDirectory(
        AgentRegistry({}),
        discovery=DiscoveryConfig(
            sources=(ManifestSource(url=f"{agent}/manifest"),), timeout=1
        ),
        health=HEALTH,
    )

    assert directory.snapshot().agent_ids() == frozenset()

    directory.refresh()

    assert directory.snapshot().agent_ids() == {"KIO2"}


def test_a_refresh_picks_up_a_capability_an_agent_gained(agent: str) -> None:
    directory = AgentDirectory(
        AgentRegistry({}),
        discovery=DiscoveryConfig(
            sources=(ManifestSource(url=f"{agent}/manifest"),), timeout=1
        ),
    )
    directory.refresh()

    AgentHandler.capabilities = ["diagnosis", "replay"]
    directory.refresh()
    record = directory.record_for("KIO2")

    assert record is not None
    assert record.endpoint.capabilities == frozenset({"diagnosis", "replay"})


def test_configuration_is_never_overwritten_while_running(agent: str) -> None:
    directory = AgentDirectory(
        kio_registry("http://written-by-hand.test:8102"),
        discovery=DiscoveryConfig(
            sources=(ManifestSource(url=f"{agent}/manifest"),), timeout=1
        ),
    )
    directory.refresh()
    record = directory.record_for("KIO2")

    assert record is not None
    assert record.origin == ORIGIN_CONFIG
    assert record.endpoint.endpoint == "http://written-by-hand.test:8102"


def test_a_refresh_does_not_wipe_what_probing_learned(agent: str) -> None:
    # The manifest keeps answering, so every pass re-reads the same entry. If
    # that reset the record, the failure count would never reach the threshold
    # and an unhealthy agent would be offered forever.
    directory = AgentDirectory(
        AgentRegistry({}),
        discovery=DiscoveryConfig(
            sources=(ManifestSource(url=f"{agent}/manifest"),), timeout=1
        ),
        health=HEALTH,
    )
    directory.refresh()

    AgentHandler.healthy = False

    for _ in range(HEALTH.failures_before_unreachable):
        directory.refresh()

    record = directory.record_for("KIO2")

    assert record is not None
    assert record.status == STATUS_UNREACHABLE
    assert "KIO2" not in directory.snapshot().agent_ids()


# ── registration ─────────────────────────────────────────────────────────────


def test_registering_an_agent_probes_it_first(agent: str) -> None:
    directory = AgentDirectory(AgentRegistry({}), health=HEALTH)
    record = directory.register(
        "KIO2", {"endpoint": agent, "capabilities": ["diagnosis"]}
    )

    assert record.status == STATUS_READY
    assert record.origin == ORIGIN_REGISTRATION
    assert "KIO2" in directory.snapshot().agent_ids()


def test_an_agent_that_does_not_answer_is_recorded_not_rejected() -> None:
    # A slow starter should join on the next pass rather than have to post
    # again, so a failed handshake is a status, not a refusal.
    directory = AgentDirectory(AgentRegistry({}), health=HEALTH)
    record = directory.register(
        "KIO9", {"endpoint": "http://127.0.0.1:1", "capabilities": ["x"]}
    )

    assert record.status == STATUS_UNREACHABLE
    assert directory.record_for("KIO9") is not None
    assert "KIO9" not in directory.snapshot().agent_ids()


def test_a_malformed_registration_is_rejected() -> None:
    directory = AgentDirectory(AgentRegistry({}), health=HEALTH)

    with pytest.raises(ValueError, match="command must be a non-empty list"):
        directory.register("Broken", {"type": "cli"})


def test_a_registered_agent_can_be_removed(agent: str) -> None:
    directory = AgentDirectory(AgentRegistry({}), health=HEALTH)
    directory.register("KIO2", {"endpoint": agent, "capabilities": ["diagnosis"]})

    assert directory.forget("KIO2") is True
    assert directory.record_for("KIO2") is None


def test_a_configured_agent_cannot_be_removed_at_runtime(agent: str) -> None:
    directory = AgentDirectory(kio_registry(agent))

    assert directory.forget("KIO2") is False
    assert directory.record_for("KIO2") is not None


def test_removing_something_unknown_reports_so() -> None:
    assert AgentDirectory(AgentRegistry({})).forget("Nobody") is False


# ── the background pass ──────────────────────────────────────────────────────


def test_an_edit_made_during_a_probe_pass_is_not_undone_by_it(
    agent: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = AgentDirectory(AgentRegistry({}), health=HEALTH)
    directory.register("KIO2", {"endpoint": agent, "capabilities": ["diagnosis"]})
    edited = load_agent_registry(
        {"KIO2": {"endpoint": agent, "capabilities": ["diagnosis", "replay"]}}
    ).resolve("KIO2")
    real_probe = directory_module.probe_target

    def probe_then_edit(target: Any, *args: Any) -> Any:
        # The console saves while the pass is still waiting on the network.
        outcome = real_probe(target, *args)
        directory.upsert(edited, ORIGIN_CONFIG, replace=True)
        return outcome

    monkeypatch.setattr(directory_module, "probe_target", probe_then_edit)
    directory._probe_all()
    record = directory.record_for("KIO2")

    assert record is not None
    assert record.endpoint == edited


def test_the_watch_keeps_the_directory_current_on_its_own(agent: str) -> None:
    directory = AgentDirectory(
        AgentRegistry({}),
        discovery=DiscoveryConfig(
            sources=(ManifestSource(url=f"{agent}/manifest"),), timeout=1
        ),
        health=HealthConfig(enabled=True, interval=0.05, timeout=1),
    )
    directory.start()

    try:
        assert directory.watching
        # The first pass is synchronous, so the first plan is never built
        # against an unchecked directory.
        assert directory.snapshot().agent_ids() == {"KIO2"}
    finally:
        directory.stop()

    assert not directory.watching


def test_nothing_is_started_when_there_is_nothing_to_do() -> None:
    directory = AgentDirectory(AgentRegistry({}))
    directory.start()

    assert not directory.watching


# ── the registry API ─────────────────────────────────────────────────────────


def make_client(directory: AgentDirectory) -> TestClient:
    """Return a client whose state holds this directory and nothing real."""
    app.dependency_overrides[get_state] = lambda: _State(
        runner=None,  # type: ignore[arg-type]
        session_client=None,  # type: ignore[arg-type]
        directory=directory,
        scoreboard=Scoreboard(),
    )

    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear_overrides() -> Iterator[None]:
    yield
    app.dependency_overrides.clear()


def test_the_registry_api_is_closed_until_a_token_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An open endpoint would let anyone on the network route task text and
    # upstream results to an address they chose.
    monkeypatch.delenv(REGISTRY_TOKEN_ENV, raising=False)
    response = make_client(AgentDirectory(AgentRegistry({}))).get("/registry/agents")

    assert response.status_code == 503
    assert REGISTRY_TOKEN_ENV in response.json()["detail"]


def test_a_wrong_token_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(REGISTRY_TOKEN_ENV, "right")
    response = make_client(AgentDirectory(AgentRegistry({}))).get(
        "/registry/agents", headers={"Authorization": "Bearer wrong"}
    )

    assert response.status_code == 401


def test_an_agent_can_register_itself_with_a_running_orchestrator(
    agent: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(REGISTRY_TOKEN_ENV, "shared")
    directory = AgentDirectory(AgentRegistry({}), health=HEALTH)
    client = make_client(directory)

    response = client.post(
        "/registry/agents",
        headers={"Authorization": "Bearer shared"},
        json={
            "manifest": {
                "agent_id": "KIO2",
                "description": "Finds bugs.",
                "endpoint": agent,
                "capabilities": ["diagnosis"],
            }
        },
    )

    assert response.status_code == 201
    assert response.json()["agents"][0]["status"] == STATUS_READY
    assert "KIO2" in directory.snapshot().agent_ids()


def test_registering_reports_an_agent_that_did_not_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(REGISTRY_TOKEN_ENV, "shared")
    client = make_client(AgentDirectory(AgentRegistry({}), health=HEALTH))

    response = client.post(
        "/registry/agents",
        headers={"Authorization": "Bearer shared"},
        json={
            "manifest": {
                "agent_id": "KIO9",
                "endpoint": "http://127.0.0.1:1",
                "capabilities": ["x"],
            }
        },
    )

    assert response.status_code == 201
    assert response.json()["agents"][0]["status"] == STATUS_UNREACHABLE


def test_a_manifest_that_names_nothing_is_a_bad_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(REGISTRY_TOKEN_ENV, "shared")
    client = make_client(AgentDirectory(AgentRegistry({})))

    response = client.post(
        "/registry/agents",
        headers={"Authorization": "Bearer shared"},
        json={"manifest": []},
    )

    assert response.status_code == 400


def test_listing_shows_why_an_agent_is_not_being_planned_for(
    agent: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(REGISTRY_TOKEN_ENV, "shared")
    directory = AgentDirectory(kio_registry(agent), health=HEALTH)
    AgentHandler.alive = False

    for _ in range(HEALTH.failures_before_unreachable):
        directory.refresh()

    body = (
        make_client(directory)
        .get("/registry/agents", headers={"Authorization": "Bearer shared"})
        .json()
    )
    record = body["agents"][0]

    assert record["status"] == STATUS_UNREACHABLE
    assert "503" in record["last_error"]
    assert record["origin"] == ORIGIN_CONFIG


def test_a_registered_agent_can_be_removed_over_the_api(
    agent: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(REGISTRY_TOKEN_ENV, "shared")
    directory = AgentDirectory(AgentRegistry({}), health=HEALTH)
    directory.register(
        "KIO2", {"endpoint": agent, "capabilities": ["diagnosis"]}, ORIGIN_DISCOVERY
    )

    response = make_client(directory).delete(
        "/registry/agents/KIO2", headers={"Authorization": "Bearer shared"}
    )

    assert response.status_code == 200
    assert directory.record_for("KIO2") is None


def test_removing_a_configured_agent_takes_it_out_of_the_file(
    agent: str, monkeypatch: pytest.MonkeyPatch, writable_config: Path
) -> None:
    # A person removing a target in the console is doing what they would have
    # done by editing the file, so it comes out of the file too — otherwise it
    # would be back at the next restart.
    monkeypatch.setenv(REGISTRY_TOKEN_ENV, "shared")
    write_agents(
        writable_config, {"KIO2": {"endpoint": agent, "capabilities": ["diagnosis"]}}
    )
    directory = AgentDirectory(kio_registry(agent))

    response = make_client(directory).delete(
        "/registry/agents/KIO2", headers={"Authorization": "Bearer shared"}
    )

    assert response.status_code == 200
    assert directory.record_for("KIO2") is None
    assert "KIO2" not in read_saved(writable_config)


def test_removing_an_unknown_agent_is_a_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(REGISTRY_TOKEN_ENV, "shared")
    response = make_client(AgentDirectory(AgentRegistry({}))).delete(
        "/registry/agents/Nobody", headers={"Authorization": "Bearer shared"}
    )

    assert response.status_code == 404


# ── configuration ────────────────────────────────────────────────────────────


def test_health_checks_are_off_unless_configured() -> None:
    assert load_health_config(None).enabled is False


def test_health_settings_are_read() -> None:
    health = load_health_config(
        {
            "enabled": True,
            "interval": 15,
            "timeout": 2,
            "failures_before_unreachable": 3,
            "path": "/live",
        }
    )

    assert health.enabled is True
    assert health.interval == 15
    assert health.failures_before_unreachable == 3
    assert health.path == "/live"


def test_a_zero_threshold_is_rejected() -> None:
    with pytest.raises(ValueError, match="must be a positive integer"):
        load_health_config({"failures_before_unreachable": 0})


def test_a_health_path_must_look_like_a_path() -> None:
    with pytest.raises(ValueError, match="must be a string starting with"):
        load_health_config({"path": "health"})


# ── saving what the console changed ─────────────────────────────────────────


def test_registering_saves_the_target_into_the_configuration(
    agent: str, monkeypatch: pytest.MonkeyPatch, writable_config: Path
) -> None:
    monkeypatch.setenv(REGISTRY_TOKEN_ENV, "shared")
    directory = AgentDirectory(AgentRegistry({}), health=HEALTH)

    response = make_client(directory).post(
        "/registry/agents",
        headers={"Authorization": "Bearer shared"},
        json={
            "manifest": {
                "agent_id": "KIO2",
                "endpoint": agent,
                "capabilities": ["diagnosis"],
            }
        },
    )

    assert response.status_code == 201
    assert read_saved(writable_config)["KIO2"]["endpoint"] == agent
    # It is in the file now, so it is a configured entry.
    record = directory.record_for("KIO2")
    assert record is not None and record.origin == ORIGIN_CONFIG


def test_a_registration_can_be_kept_out_of_the_configuration(
    agent: str, monkeypatch: pytest.MonkeyPatch, writable_config: Path
) -> None:
    monkeypatch.setenv(REGISTRY_TOKEN_ENV, "shared")
    before = read_saved(writable_config)

    make_client(AgentDirectory(AgentRegistry({}), health=HEALTH)).post(
        "/registry/agents",
        headers={"Authorization": "Bearer shared"},
        json={
            "manifest": {
                "agent_id": "Ephemeral",
                "endpoint": agent,
                "capabilities": ["diagnosis"],
            },
            "persist": False,
        },
    )

    assert read_saved(writable_config) == before


def test_editing_a_target_replaces_it_everywhere(
    agent: str, monkeypatch: pytest.MonkeyPatch, writable_config: Path
) -> None:
    monkeypatch.setenv(REGISTRY_TOKEN_ENV, "shared")
    write_agents(
        writable_config, {"KIO2": {"endpoint": agent, "capabilities": ["diagnosis"]}}
    )
    directory = AgentDirectory(kio_registry(agent), health=HEALTH)

    response = make_client(directory).put(
        "/registry/agents/KIO2",
        headers={"Authorization": "Bearer shared"},
        json={
            "entry": {
                "endpoint": agent,
                "description": "Now it says what it does.",
                "capabilities": ["diagnosis", "replay"],
            }
        },
    )

    assert response.status_code == 200
    record = directory.record_for("KIO2")
    assert record is not None
    assert record.endpoint.capabilities == frozenset({"diagnosis", "replay"})
    assert record.endpoint.description == "Now it says what it does."
    assert read_saved(writable_config)["KIO2"]["capabilities"] == [
        "diagnosis",
        "replay",
    ]


def test_a_malformed_edit_changes_nothing(
    agent: str, monkeypatch: pytest.MonkeyPatch, writable_config: Path
) -> None:
    monkeypatch.setenv(REGISTRY_TOKEN_ENV, "shared")
    write_agents(
        writable_config, {"KIO2": {"endpoint": agent, "capabilities": ["diagnosis"]}}
    )
    directory = AgentDirectory(kio_registry(agent), health=HEALTH)

    response = make_client(directory).put(
        "/registry/agents/KIO2",
        headers={"Authorization": "Bearer shared"},
        json={"entry": {"type": "cli"}},
    )

    assert response.status_code == 400
    assert read_saved(writable_config)["KIO2"]["capabilities"] == ["diagnosis"]


def test_another_targets_definition_is_not_saved_under_this_name(
    agent: str, monkeypatch: pytest.MonkeyPatch, writable_config: Path
) -> None:
    # What happened to a real configuration: TimeLookup's definition was
    # pasted while editing WeatherForecast, and saved as WeatherForecast.
    monkeypatch.setenv(REGISTRY_TOKEN_ENV, "shared")
    write_agents(
        writable_config, {"KIO2": {"endpoint": agent, "capabilities": ["diagnosis"]}}
    )
    directory = AgentDirectory(kio_registry(agent), health=HEALTH)

    response = make_client(directory).put(
        "/registry/agents/KIO2",
        headers={"Authorization": "Bearer shared"},
        json={"entry": {"agent_id": "KIO3", "endpoint": agent, "capabilities": ["x"]}},
    )

    assert response.status_code == 400
    assert "This definition is for KIO3, not KIO2" in response.json()["detail"]
    assert read_saved(writable_config)["KIO2"]["capabilities"] == ["diagnosis"]


def test_a_record_carries_the_entry_it_was_built_from() -> None:
    # The console needs the definition back to put it in an edit form.
    entry = {"endpoint": "http://127.0.0.1:8102", "capabilities": ["diagnosis"]}
    directory = AgentDirectory(
        load_agent_registry({"KIO2": entry}), sources={"KIO2": entry}
    )
    record = directory.record_for("KIO2")

    assert record is not None
    assert record.to_summary()["source"] == entry


def test_the_console_is_told_where_saves_go(
    monkeypatch: pytest.MonkeyPatch, writable_config: Path
) -> None:
    monkeypatch.setenv(REGISTRY_TOKEN_ENV, "shared")

    body = (
        make_client(AgentDirectory(AgentRegistry({})))
        .get("/registry/config", headers={"Authorization": "Bearer shared"})
        .json()
    )

    assert body["config_path"] == str(writable_config)
    assert body["writable"] is True


def test_saving_while_it_is_turned_off_is_reported(
    agent: str, monkeypatch: pytest.MonkeyPatch, writable_config: Path
) -> None:
    # A read-only deployment should say so, not fail obscurely.
    monkeypatch.setenv(REGISTRY_TOKEN_ENV, "shared")
    monkeypatch.setenv("KIO1_CONFIG_WRITABLE", "0")

    response = make_client(AgentDirectory(AgentRegistry({}), health=HEALTH)).post(
        "/registry/agents",
        headers={"Authorization": "Bearer shared"},
        json={
            "manifest": {
                "agent_id": "KIO2",
                "endpoint": agent,
                "capabilities": ["diagnosis"],
            }
        },
    )

    assert response.status_code == 503
    assert "KIO1_CONFIG_WRITABLE" in response.json()["detail"]


# ── trying a definition before committing to it ─────────────────────────────


def test_a_probe_reports_a_target_that_answers(
    agent: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(REGISTRY_TOKEN_ENV, "shared")

    body = (
        make_client(AgentDirectory(AgentRegistry({}), health=HEALTH))
        .post(
            "/registry/agents/probe",
            headers={"Authorization": "Bearer shared"},
            json={
                "entry": {
                    "agent_id": "KIO2",
                    "endpoint": agent,
                    "capabilities": ["diagnosis"],
                }
            },
        )
        .json()
    )

    assert body["status"] == STATUS_READY
    assert body["kind"] == "kio"
    assert body["capabilities"] == ["diagnosis"]
    assert body["probed"].endswith("/health")


def test_a_probe_reports_why_a_target_does_not_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(REGISTRY_TOKEN_ENV, "shared")

    body = (
        make_client(AgentDirectory(AgentRegistry({}), health=HEALTH))
        .post(
            "/registry/agents/probe",
            headers={"Authorization": "Bearer shared"},
            json={
                "entry": {
                    "agent_id": "Nope",
                    "endpoint": "http://127.0.0.1:1",
                    "capabilities": ["x"],
                }
            },
        )
        .json()
    )

    assert body["status"] == STATUS_UNREACHABLE
    assert "127.0.0.1:1" in body["detail"]


def test_a_probe_says_when_there_is_nothing_to_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(REGISTRY_TOKEN_ENV, "shared")

    body = (
        make_client(AgentDirectory(AgentRegistry({}), health=HEALTH))
        .post(
            "/registry/agents/probe",
            headers={"Authorization": "Bearer shared"},
            json={
                "entry": {
                    "agent_id": "Api",
                    "type": "http",
                    "url": "https://example.test/run",
                    "capabilities": ["x"],
                }
            },
        )
        .json()
    )

    assert body["status"] == STATUS_UNCHECKED
    assert body["probed"] == ""


def test_a_probe_shows_the_address_this_deployment_will_really_dial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The thing a person cannot see for themselves: inside a container their
    # loopback address is rewritten, and the probe says so.
    monkeypatch.setenv(REGISTRY_TOKEN_ENV, "shared")
    monkeypatch.setenv("KIO1_AGENT_HOST", "host.docker.internal")

    body = (
        make_client(AgentDirectory(AgentRegistry({}), health=HEALTH))
        .post(
            "/registry/agents/probe",
            headers={"Authorization": "Bearer shared"},
            json={
                "entry": {
                    "agent_id": "Ollama",
                    "type": "http",
                    "url": "http://127.0.0.1:11434/api/generate",
                    "health_url": "http://127.0.0.1:11434/",
                    "capabilities": ["llm_inference"],
                }
            },
        )
        .json()
    )

    assert body["probed"] == "http://host.docker.internal:11434/"
    assert body["rehosted_from"] == "http://127.0.0.1:11434/"


def test_a_probe_changes_nothing(agent: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(REGISTRY_TOKEN_ENV, "shared")
    directory = AgentDirectory(AgentRegistry({}), health=HEALTH)

    make_client(directory).post(
        "/registry/agents/probe",
        headers={"Authorization": "Bearer shared"},
        json={
            "entry": {
                "agent_id": "KIO2",
                "endpoint": agent,
                "capabilities": ["diagnosis"],
            }
        },
    )

    assert directory.record_for("KIO2") is None


def test_a_malformed_definition_is_caught_before_saving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(REGISTRY_TOKEN_ENV, "shared")

    response = make_client(AgentDirectory(AgentRegistry({}))).post(
        "/registry/agents/probe",
        headers={"Authorization": "Bearer shared"},
        json={"entry": {"agent_id": "Broken", "type": "cli"}},
    )

    assert response.status_code == 400
    assert "command must be a non-empty list" in response.json()["detail"]
