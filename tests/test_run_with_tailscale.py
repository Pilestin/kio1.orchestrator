"""Tests for run_with_tailscale.py: one command, no options, two ways to share.

The properties that matter: on the tailnet the console listens on this
machine's Tailscale address under its Tailscale name; through Funnel it
listens on 127.0.0.1 only, is published at the machine's https://…ts.net
address, and the Funnel comes down when KIO1 stops; the choice is asked once
and kept in .env; the planner points at the model machine unless .env says
otherwise; and nothing starts without a password when nobody can be asked.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import run_with_tailscale as launcher
import start
from core import console_auth
from core.console_auth import PASSWORD_HASH_ENV, hash_password
from core.dotenv import parse_dotenv

HERE = "100.75.109.86"
DNS_NAME = "opeva-cloud-server.tail1234.ts.net"
KEPT = (
    "KIO1_HOST",
    "KIO1_PROVIDER_ENDPOINT",
    "KIO1_CONSOLE_NAME",
    "KIO1_PUBLIC_URL",
    "KIO1_SHARE",
)


@pytest.fixture
def launched(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[dict]:
    seen: dict[str, Any] = {"funnel": [], "off": 0}
    dotenv = tmp_path / ".env"
    monkeypatch.setattr(launcher, "DOTENV", dotenv)
    monkeypatch.setattr(launcher, "tailscale_address", lambda: HERE)
    monkeypatch.setattr(launcher, "tailscale_dns_name", lambda: DNS_NAME)
    monkeypatch.setattr(
        launcher, "funnel", lambda port: (seen["funnel"].append(port) or True, "")
    )
    monkeypatch.setattr(
        launcher, "funnel_off", lambda: seen.update(off=seen["off"] + 1)
    )
    monkeypatch.setattr(start, "say", lambda text: None)
    monkeypatch.setattr(start, "free_port", lambda host, first: first)
    monkeypatch.setattr(start, "_get_json", lambda url, timeout: {"models": []})
    monkeypatch.setattr(
        start, "main", lambda: seen.update(argv=list(launcher.sys.argv))
    )
    monkeypatch.setattr(launcher.sys.stdin, "isatty", lambda: False)
    # Set, then removed, so monkeypatch undoes what the launcher writes.
    for name in KEPT:
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    monkeypatch.setenv(PASSWORD_HASH_ENV, hash_password("a long enough password"))
    # The launcher rewrites sys.argv for start.main; put it back afterwards.
    monkeypatch.setattr(launcher.sys, "argv", ["run_with_tailscale.py"])
    seen["dotenv"] = dotenv
    yield seen


def kept(seen: dict) -> dict[str, str]:
    return parse_dotenv(seen["dotenv"].read_text(encoding="utf-8"))


def run(*arguments: str) -> None:
    launcher.sys.argv = ["run_with_tailscale.py", *arguments]
    launcher.main()


# ── on the tailnet ──────────────────────────────────────────────────────────


def test_on_the_tailnet_the_console_listens_on_the_tailscale_address(
    launched: dict,
) -> None:
    run()

    assert kept(launched)["KIO1_HOST"] == HERE
    assert kept(launched)["KIO1_CONSOLE_NAME"] == "opeva-cloud-server"
    assert launched["argv"][1:] == ["--no-browser", "--port", "8101"]
    assert launched["funnel"] == []


def test_the_planner_points_at_the_model_machine(launched: dict) -> None:
    run()

    assert kept(launched)["KIO1_PROVIDER_ENDPOINT"] == "http://100.71.36.18:11434"


def test_an_endpoint_already_in_dotenv_is_kept(
    launched: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KIO1_PROVIDER_ENDPOINT", "http://100.99.0.1:11434")

    run()

    assert "KIO1_PROVIDER_ENDPOINT" not in kept(launched)


def test_without_a_password_and_nobody_to_ask_it_stops(
    launched: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(PASSWORD_HASH_ENV)

    with pytest.raises(SystemExit):
        run()

    assert "argv" not in launched


# ── through Funnel ──────────────────────────────────────────────────────────


def test_through_funnel_kio1_listens_on_this_machine_only(launched: dict) -> None:
    run("--funnel")

    assert kept(launched)["KIO1_HOST"] == "127.0.0.1"
    assert kept(launched)["KIO1_PUBLIC_URL"] == f"https://{DNS_NAME}"
    assert launched["funnel"] == [8101]


def test_the_funnel_comes_down_when_kio1_stops(launched: dict) -> None:
    run("--funnel")

    # Once before, for whatever an earlier run left; once after.
    assert launched["off"] == 2


def test_the_choice_is_kept_so_the_next_start_needs_no_option(
    launched: dict,
) -> None:
    run("--funnel")
    launched["funnel"].clear()

    run()

    assert kept(launched)["KIO1_SHARE"] == "funnel"
    assert launched["funnel"] == [8101]


def test_switching_back_to_the_tailnet_unpublishes_the_link(launched: dict) -> None:
    run("--funnel")
    run("--tailnet")

    assert kept(launched)["KIO1_PUBLIC_URL"] == ""
    assert kept(launched)["KIO1_HOST"] == HERE


def test_a_funnel_that_cannot_start_stops_kio1_too(
    launched: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(launcher, "funnel", lambda port: (False, "not allowed"))

    with pytest.raises(SystemExit):
        run("--funnel")

    assert "argv" not in launched


def test_a_refused_user_is_allowed_once_and_the_funnel_tried_again(
    launched: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = iter([(False, "Access denied: serve config denied"), (True, "")])
    monkeypatch.setattr(launcher, "funnel", lambda port: next(answers))
    monkeypatch.setattr(launcher, "allow_this_user", lambda: True)

    run("--funnel")

    assert "argv" in launched


def test_without_the_persons_yes_nothing_runs_as_root(
    launched: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        launcher.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("ran a command without asking"),
    )

    # Nobody at the keyboard: no question, no sudo.
    assert launcher.allow_this_user() is False


# ── start.py and the service ────────────────────────────────────────────────


def start_main_up_to_the_password_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PASSWORD_HASH_ENV, raising=False)
    monkeypatch.setattr(start, "say", lambda text: None)
    for step in ("ensure_python", "ensure_config", "ensure_token", "protect_dotenv"):
        monkeypatch.setattr(start, step, lambda *args: {})
    monkeypatch.setattr(start, "ensure_environment", lambda skip: Path("python"))
    monkeypatch.setattr(start, "load_dotenv", lambda path: None)
    monkeypatch.setattr(start.sys, "argv", ["start.py"])


def test_start_listens_where_dotenv_says(monkeypatch: pytest.MonkeyPatch) -> None:
    start_main_up_to_the_password_check(monkeypatch)
    monkeypatch.setenv("KIO1_HOST", HERE)

    # No password: it refuses — which shows the host came from KIO1_HOST.
    with pytest.raises(SystemExit):
        start.main()


def test_start_will_not_publish_without_a_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start_main_up_to_the_password_check(monkeypatch)
    monkeypatch.setenv("KIO1_HOST", "127.0.0.1")
    monkeypatch.setenv("KIO1_PUBLIC_URL", f"https://{DNS_NAME}")

    with pytest.raises(SystemExit):
        start.main()


def test_visitors_without_an_address_cannot_lock_each_other_out() -> None:
    gate = console_auth.Gate()
    for _ in range(console_auth.FAILURES_PER_ADDRESS * 2):
        gate.failed("")

    assert gate.wait_before_trying("") == 0
