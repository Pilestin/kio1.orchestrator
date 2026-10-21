"""Tests for starting KIO1 on a machine that has nothing but Python.

A first run must not need Docker, a database server, a hand-written
configuration file, or a secret chosen by the person running it. Each test
here removes one of those requirements.
"""

import json
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

import start
from core.config_loader import Config
from core.config_writer import EXAMPLE_CONFIG, ensure_config
from core.session_client import SessionClientError
from providers.ollama_client import _get_think
from services.session_manager.repository import InMemorySessionRepository
from services.session_manager.sqlite_repository import SqliteSessionRepository
from services.session_manager.storage import EmbeddedSessionClient, open_repository

# ── configuration ───────────────────────────────────────────────────────────


def test_a_missing_configuration_is_created_from_the_example(tmp_path: Path) -> None:
    target = tmp_path / "config.json"

    assert ensure_config(target) is True
    assert json.loads(target.read_text(encoding="utf-8")) == json.loads(
        EXAMPLE_CONFIG.read_text(encoding="utf-8")
    )


def test_an_existing_configuration_is_never_overwritten(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    target.write_text('{"model": "mine"}', encoding="utf-8")

    assert ensure_config(target) is False
    assert target.read_text(encoding="utf-8") == '{"model": "mine"}'


def test_the_example_holds_nothing_personal() -> None:
    # The file is shared with everyone who clones the repository.
    document = json.loads(EXAMPLE_CONFIG.read_text(encoding="utf-8"))

    assert document["dispatch"]["workspace"]["path"] == ""
    assert "KIO1_REGISTRY_TOKEN" not in EXAMPLE_CONFIG.read_text(encoding="utf-8")


# ── storage without a server ────────────────────────────────────────────────


def test_a_sqlite_url_opens_a_file(tmp_path: Path) -> None:
    repository, close = open_repository(f"sqlite:///{tmp_path.as_posix()}/kio1.db")

    try:
        assert isinstance(repository, SqliteSessionRepository)
        assert (tmp_path / "kio1.db").exists()
    finally:
        close()


def test_no_url_keeps_sessions_in_memory() -> None:
    repository, close = open_repository("")
    close()

    assert isinstance(repository, InMemorySessionRepository)


@pytest.fixture
def embedded(tmp_path: Path) -> Iterator[EmbeddedSessionClient]:
    """Return a client over a fresh SQLite file."""
    client = EmbeddedSessionClient(*open_repository(f"sqlite:///{tmp_path}/s.db"))
    yield client
    client.close()


def test_a_run_is_recorded_and_read_back_like_the_service_returns_it(
    embedded: EmbeddedSessionClient,
) -> None:
    session_id = embedded.create_session("Diagnose the crash")
    embedded.update_session(
        session_id, status="running", workflow_id="wf-1", execution_mode="sequential"
    )
    embedded.record_step(
        session_id,
        {
            "step_id": "s1",
            "agent_id": "KIO2",
            "capability": "diagnosis",
            "task": "Find it",
            "status": "ok",
            "duration_ms": 5,
            "output": {"summary": "found"},
            "depends_on": [],
        },
    )

    detail = embedded.get_session(session_id)

    # JSON-ready, exactly what the HTTP Session Manager would have sent.
    json.dumps(detail)
    assert detail["session"]["status"] == "running"
    assert detail["steps"][0]["output"] == {"summary": "found"}
    assert embedded.list_sessions(5)[0]["session_id"] == str(session_id)


def test_reading_an_unknown_run_raises_like_the_remote_client(
    embedded: EmbeddedSessionClient,
) -> None:
    with pytest.raises(SessionClientError):
        embedded.get_session(uuid4())


def test_a_bad_step_record_never_interrupts_a_workflow(
    embedded: EmbeddedSessionClient,
) -> None:
    session_id = embedded.create_session("Anything")

    embedded.record_step(session_id, {"step_id": "s1"})
    embedded.update_session(session_id, status="not-a-status")

    assert embedded.get_session(session_id)["steps"] == []


def test_history_survives_a_restart(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path}/kio1.db"
    first = EmbeddedSessionClient(*open_repository(url))
    session_id = first.create_session("Remember me")
    first.close()

    second = EmbeddedSessionClient(*open_repository(url))

    try:
        assert (
            second.get_session(session_id)["session"]["request_text"] == "Remember me"
        )
    finally:
        second.close()


# ── the launcher ────────────────────────────────────────────────────────────


def test_the_token_is_generated_once_and_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KIO1_REGISTRY_TOKEN", raising=False)
    monkeypatch.setattr(start, "ROOT", tmp_path)
    monkeypatch.setattr(start, "DATA", tmp_path / "data")
    monkeypatch.setattr(start, "TOKEN_FILE", tmp_path / "data" / "registry-token")

    first = start.ensure_token()

    assert len(first) >= 24
    assert start.ensure_token() == first


def test_a_token_set_by_the_operator_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KIO1_REGISTRY_TOKEN", "chosen")
    monkeypatch.setattr(start, "TOKEN_FILE", tmp_path / "registry-token")

    assert start.ensure_token() == "chosen"
    assert not (tmp_path / "registry-token").exists()


def test_a_busy_port_is_skipped() -> None:
    import socket

    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        taken = busy.getsockname()[1]

        assert start.free_port("127.0.0.1", taken) != taken


# ── the model ───────────────────────────────────────────────────────────────


def ollama_config(**options: object) -> Config:
    """Return a configuration with the given provider options."""
    return Config(
        provider="ollama",
        allowed_providers=frozenset({"ollama"}),
        model="m",
        prompt_path="p",
        chat_directory="c",
        temperature=0.2,
        request_timeout=30,
        keep_alive=-1,
        max_output_tokens=1024,
        provider_options=dict(options),
    )


def test_a_reasoning_model_does_not_think_unless_asked() -> None:
    assert _get_think(ollama_config()) is False
    assert _get_think(ollama_config(think=True)) is True


def test_think_must_be_a_boolean() -> None:
    with pytest.raises(ValueError, match="think"):
        _get_think(ollama_config(think="yes"))
