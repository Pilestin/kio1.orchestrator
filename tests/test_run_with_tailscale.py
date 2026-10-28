"""Tests for run_with_tailscale.py: one command, no options, on the tailnet.

The properties that matter: the console listens on this machine's Tailscale
address and that choice is kept in .env, so a plain start.py does the same;
the planner points at the model machine unless .env already says otherwise;
and a start with no password and nobody at the keyboard stops instead of
opening an unlocked console.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

import run_with_tailscale as launcher
import start
from core.console_auth import PASSWORD_HASH_ENV, hash_password
from core.dotenv import parse_dotenv

HERE = "100.80.1.2"


@pytest.fixture
def launched(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[dict]:
    seen: dict = {}
    dotenv = tmp_path / ".env"
    monkeypatch.setattr(launcher, "DOTENV", dotenv)
    monkeypatch.setattr(launcher, "tailscale_address", lambda: HERE)
    monkeypatch.setattr(start, "say", lambda text: None)
    monkeypatch.setattr(start, "_get_json", lambda url, timeout: {"models": []})
    monkeypatch.setattr(
        start, "main", lambda: seen.update(argv=list(launcher.sys.argv))
    )
    for name in ("KIO1_HOST", "KIO1_PROVIDER_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(PASSWORD_HASH_ENV, hash_password("a long enough password"))
    # The launcher rewrites sys.argv for start.main; put it back afterwards.
    monkeypatch.setattr(launcher.sys, "argv", list(launcher.sys.argv))
    seen["dotenv"] = dotenv
    yield seen


def kept(seen: dict) -> dict[str, str]:
    return parse_dotenv(seen["dotenv"].read_text(encoding="utf-8"))


def test_the_console_listens_on_the_tailnet_and_remembers_it(launched: dict) -> None:
    launcher.main()

    assert kept(launched)["KIO1_HOST"] == HERE
    assert launched["argv"][1:] == ["--no-browser"]


def test_the_planner_points_at_the_model_machine(launched: dict) -> None:
    launcher.main()

    assert kept(launched)["KIO1_PROVIDER_ENDPOINT"] == "http://100.71.36.18:11434"


def test_an_endpoint_already_in_dotenv_is_kept(
    launched: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KIO1_PROVIDER_ENDPOINT", "http://100.99.0.1:11434")

    launcher.main()

    assert "KIO1_PROVIDER_ENDPOINT" not in kept(launched)


def test_without_a_password_and_nobody_to_ask_it_stops(
    launched: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(PASSWORD_HASH_ENV)
    monkeypatch.setattr(launcher.sys.stdin, "isatty", lambda: False)

    with pytest.raises(SystemExit):
        launcher.main()

    assert "argv" not in launched


def test_start_listens_where_dotenv_says(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KIO1_HOST", HERE)
    monkeypatch.delenv(PASSWORD_HASH_ENV, raising=False)
    monkeypatch.setattr(start, "say", lambda text: None)
    for step in ("ensure_python", "ensure_config", "ensure_token", "protect_dotenv"):
        monkeypatch.setattr(start, step, lambda *args: {})
    monkeypatch.setattr(start, "ensure_environment", lambda skip: Path("python"))
    monkeypatch.setattr(start, "load_dotenv", lambda path: None)
    monkeypatch.setattr(start.sys, "argv", ["start.py"])

    # No password: it refuses — which shows the host came from KIO1_HOST.
    with pytest.raises(SystemExit):
        start.main()
