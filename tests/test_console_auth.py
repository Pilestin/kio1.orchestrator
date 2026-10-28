"""Tests for the console password.

The properties that matter: with a password set, a stranger reaches nothing
but the sign-in page and the health check — no workflow, no conversation, no
file a scanner asks for; the right password opens a session the browser keeps
in an HttpOnly cookie; guessing is cut off after a few tries; signing in is
enough for the administration pages; and with no password, nothing changes.
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app import _State, app, get_state
from core import console_auth
from core.console_auth import Gate, hash_password, verify_password
from core.session_client import NullSessionClient
from dispatch.agent_registry import AgentRegistry
from dispatch.directory import AgentDirectory
from dispatch.scoreboard import Scoreboard
from tests.test_settings_api import make_config

PASSWORD = "a long enough password"


@pytest.fixture(scope="module")
def stored() -> str:
    return hash_password(PASSWORD)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, stored: str) -> Iterator[TestClient]:
    monkeypatch.setenv(console_auth.PASSWORD_HASH_ENV, stored)
    monkeypatch.setenv("KIO1_REGISTRY_TOKEN", "admin-token")
    monkeypatch.setattr(console_auth, "GATE", Gate())
    # Without `with`: the startup work (agents, health checks) is not what
    # these tests are about, and the lock does not need it.
    yield TestClient(app)


@pytest.fixture
def started(client: TestClient) -> Iterator[TestClient]:
    """The same, with a service state behind it: for pages that read one.

    A state made here rather than the real startup, which would read this
    machine's config.json and .env and fail wherever a key is missing.
    """
    state = _State(
        runner=type("Runner", (), {"config": make_config()})(),  # type: ignore[arg-type]
        session_client=NullSessionClient(),
        directory=AgentDirectory(AgentRegistry({})),
        scoreboard=Scoreboard(),
    )
    app.dependency_overrides[get_state] = lambda: state
    yield client
    app.dependency_overrides.clear()


def sign_in(client: TestClient, password: str = PASSWORD) -> int:
    return client.post("/login", json={"password": password}).status_code


def test_a_password_is_kept_only_as_a_hash_that_checks_it(stored: str) -> None:
    assert PASSWORD not in stored
    assert verify_password(PASSWORD, stored)
    assert not verify_password("a long enough passworD", stored)


def test_a_stranger_cannot_start_a_workflow(client: TestClient) -> None:
    response = client.post("/workflow/run", json={"request": "Run the tests"})

    assert response.status_code == 401
    assert response.json()["sign_in"] == "/login"


def test_a_stranger_cannot_read_the_conversations(client: TestClient) -> None:
    assert client.get("/conversations").status_code == 401


@pytest.mark.parametrize("path", ["/.env", "/config.json", "/docs", "/static/app.js"])
def test_a_scanner_gets_nothing_but_a_refusal(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 401


def test_a_browser_is_sent_to_the_sign_in_page(client: TestClient) -> None:
    response = client.get("/", headers={"Accept": "text/html"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_the_health_check_and_the_sign_in_page_stay_open(client: TestClient) -> None:
    assert client.get("/health").status_code == 200
    assert "Sign in to KIO1" in client.get("/login").text


def test_the_right_password_opens_a_session_in_an_httponly_cookie(
    started: TestClient,
) -> None:
    response = started.post("/login", json={"password": PASSWORD})

    cookie = response.headers["set-cookie"]
    assert response.status_code == 200
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert started.get("/conversations").status_code == 200


def test_a_wrong_password_opens_nothing(client: TestClient) -> None:
    assert sign_in(client, "not it") == 401
    assert client.get("/conversations").status_code == 401


def test_guessing_stops_after_a_few_tries(client: TestClient) -> None:
    for _ in range(console_auth.FAILURES_PER_ADDRESS):
        sign_in(client, "guess")

    assert sign_in(client) == 429


def test_signing_out_closes_the_session(client: TestClient) -> None:
    sign_in(client)
    client.post("/logout")

    assert client.get("/conversations").status_code == 401


def test_signing_in_is_enough_for_the_administration_pages(started: TestClient) -> None:
    sign_in(started)

    assert started.get("/registry/agents").status_code == 200


def test_a_script_can_still_use_the_administration_token(started: TestClient) -> None:
    response = started.get(
        "/registry/agents", headers={"Authorization": "Bearer admin-token"}
    )

    assert response.status_code == 200


def test_without_a_password_there_is_no_sign_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(console_auth.PASSWORD_HASH_ENV, raising=False)

    assert TestClient(app).get("/health").status_code == 200
    assert TestClient(app).get("/login", follow_redirects=False).status_code == 303
