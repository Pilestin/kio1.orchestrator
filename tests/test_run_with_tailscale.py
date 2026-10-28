"""Tests for run_with_tailscale.py: one command, a public link, no questions.

The properties that matter: KIO1 listens on 127.0.0.1 only and is published
at the machine's https://…ts.net address, which comes down when KIO1 stops;
a user Tailscale refuses is made its operator and the link tried again; a
missing console password is made, and kept where only this user reads it;
the planner points at the model machine unless .env says otherwise; and
start.py will not publish without a password.
"""

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import run_with_tailscale as launcher
import start
from core import console_auth
from core.console_auth import PASSWORD_HASH_ENV, hash_password, verify_password
from core.dotenv import parse_dotenv

DNS_NAME = "opeva-cloud-server.tail1234.ts.net"
STATUS = {
    "Self": {"DNSName": DNS_NAME + ".", "TailscaleIPs": ["100.75.109.86", "fd7a::1"]}
}
KEPT = ("KIO1_HOST", "KIO1_PROVIDER_ENDPOINT", "KIO1_CONSOLE_NAME", "KIO1_PUBLIC_URL")


@pytest.fixture
def launched(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[dict]:
    seen: dict[str, Any] = {"funnel": [], "off": 0, "operator": 0}
    monkeypatch.setattr(launcher, "DOTENV", tmp_path / ".env")
    monkeypatch.setattr(start, "ROOT", tmp_path)
    monkeypatch.setattr(start, "DATA", tmp_path / "data")
    monkeypatch.setattr(start, "PASSWORD_FILE", tmp_path / "data" / "console-password")
    monkeypatch.setattr(launcher, "stop_earlier_runs", lambda: None)
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    monkeypatch.setattr(launcher, "tailscale_status", lambda: STATUS)
    monkeypatch.setattr(
        launcher, "funnel", lambda port: (seen["funnel"].append(port) or True, "")
    )
    monkeypatch.setattr(
        launcher, "funnel_off", lambda: seen.update(off=seen["off"] + 1)
    )
    monkeypatch.setattr(
        launcher, "make_operator", lambda: seen.update(operator=1) or True
    )
    monkeypatch.setattr(
        start, "say", lambda text: seen.setdefault("said", []).append(text)
    )
    monkeypatch.setattr(start, "free_port", lambda host, first: first)
    monkeypatch.setattr(start, "_get_json", lambda url, timeout: {"models": []})
    monkeypatch.setattr(
        start, "main", lambda: seen.update(argv=list(launcher.sys.argv))
    )
    # Set, then removed, so monkeypatch undoes what the launcher writes.
    for name in (*KEPT, PASSWORD_HASH_ENV):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    monkeypatch.setenv(PASSWORD_HASH_ENV, hash_password("a long enough password"))
    # The launcher rewrites sys.argv for start.main; put it back afterwards.
    monkeypatch.setattr(launcher.sys, "argv", ["run_with_tailscale.py"])
    seen["dotenv"] = tmp_path / ".env"
    yield seen


def kept(seen: dict) -> dict[str, str]:
    return parse_dotenv(seen["dotenv"].read_text(encoding="utf-8"))


def test_kio1_listens_on_this_machine_only_and_is_published(launched: dict) -> None:
    launcher.main()

    assert kept(launched)["KIO1_HOST"] == "127.0.0.1"
    assert kept(launched)["KIO1_PUBLIC_URL"] == f"https://{DNS_NAME}"
    assert launched["funnel"] == [8101]
    assert launched["argv"][1:] == ["--no-browser", "--port", "8101"]


def test_the_link_comes_down_when_kio1_stops(launched: dict) -> None:
    launcher.main()

    # Once before, for whatever an earlier run left; once after.
    assert launched["off"] == 2


def test_a_refused_user_is_made_operator_and_the_link_tried_again(
    launched: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = iter([(False, "Access denied: serve config denied"), (True, "")])
    monkeypatch.setattr(launcher, "funnel", lambda port: next(answers))

    launcher.main()

    assert launched["operator"] == 1
    assert "argv" in launched


def test_a_link_that_cannot_be_published_stops_kio1_too(
    launched: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(launcher, "funnel", lambda port: (False, "not allowed"))

    with pytest.raises(SystemExit):
        launcher.main()

    assert "argv" not in launched


def test_a_missing_password_is_made_and_kept_for_this_user(
    launched: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(PASSWORD_HASH_ENV)

    launcher.main()

    password = start.PASSWORD_FILE.read_text(encoding="utf-8").strip()
    assert verify_password(password, kept(launched)[PASSWORD_HASH_ENV])
    assert any(password in line for line in launched["said"])
    if os.name == "posix":
        assert start.PASSWORD_FILE.stat().st_mode & 0o077 == 0


def test_an_existing_password_is_left_alone(launched: dict) -> None:
    launcher.main()

    assert PASSWORD_HASH_ENV not in kept(launched)
    assert not start.PASSWORD_FILE.exists()


def test_the_planner_points_at_the_model_machine(launched: dict) -> None:
    launcher.main()

    assert kept(launched)["KIO1_PROVIDER_ENDPOINT"] == "http://100.71.36.18:11434"


def test_an_endpoint_already_in_dotenv_is_kept(
    launched: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KIO1_PROVIDER_ENDPOINT", "http://100.99.0.1:11434")

    launcher.main()

    assert "KIO1_PROVIDER_ENDPOINT" not in kept(launched)


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
    monkeypatch.setenv("KIO1_HOST", "100.75.109.86")

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


def test_a_password_set_by_hand_can_be_read_back(launched: dict) -> None:
    start.keep_password("chosen by a person")

    assert (
        start.PASSWORD_FILE.read_text(encoding="utf-8").strip() == "chosen by a person"
    )
    assert verify_password("chosen by a person", kept(launched)[PASSWORD_HASH_ENV])
