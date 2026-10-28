"""Start KIO1 on this server with Tailscale, planning with the shared model machine.

    python3 run_with_tailscale.py              # every time; no options needed
    python3 run_with_tailscale.py --funnel     # once, to switch to a public link
    python3 run_with_tailscale.py --tailnet    # once, to switch back

The first time, it asks what it cannot know — who should reach the console,
and a console password — and keeps the answers in `.env`, so every later start
is the same command. Two ways to share:

- **tailnet** — the console listens on this machine's Tailscale address and
  nowhere else: devices signed in to your tailnet reach it at
  `http://<name>:8101`; the internet sees no open port.
- **funnel** — for people without Tailscale. KIO1 listens on 127.0.0.1 only,
  and Tailscale Funnel publishes it at `https://<name>.<tailnet>.ts.net`, with
  a certificate Tailscale manages. Anyone with the link reaches the sign-in
  page, so the password is required, the session cookie is HTTPS-only, and
  the Funnel is switched off again when KIO1 stops.

Either way the planner uses Ollama on the model machine (`MODEL_MACHINE`, over
Tailscale), unless `.env` already names another `KIO1_PROVIDER_ENDPOINT`.

See docs/deploy.md for the model machine's side and for running this as a
service.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import start
from core.console_auth import PASSWORD_HASH_ENV
from core.dotenv import load_dotenv, save_dotenv_value

# The shared machine that serves the models, by its Tailscale address.
MODEL_MACHINE = "100.71.36.18"
OLLAMA_PORT = 11434
DOTENV = ROOT / ".env"
SHARE_ENV = "KIO1_SHARE"
MODES = ("tailnet", "funnel")


def tailscale() -> str:
    """The `tailscale` command, or exit saying how to install it."""
    executable = shutil.which("tailscale")
    if executable is None:
        start.say("Tailscale is not installed here. Install it:")
        start.say(
            "    curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up"
        )
        sys.exit(1)
    return executable


def tailscale_address() -> str:
    """This machine's Tailscale IPv4 address, or exit saying how to get one."""
    try:
        found = subprocess.run(
            [tailscale(), "ip", "-4"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        found = None

    address = (
        found.stdout.strip().splitlines()[0] if found and found.stdout.strip() else ""
    )
    if not address:
        start.say("Tailscale is installed but not connected. Run:  sudo tailscale up")
        sys.exit(1)
    return address


def tailscale_dns_name() -> str:
    """This machine's full name on the tailnet (`opeva-cloud-server.tail1234.ts.net`), or ""."""
    try:
        found = subprocess.run(
            [tailscale(), "status", "--json"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        dns_name = json.loads(found.stdout).get("Self", {}).get("DNSName", "")
    except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError):
        return ""
    return str(dns_name).rstrip(".")


def remember(name: str, value: str) -> None:
    """Keep a value in .env and in this process."""
    if os.environ.get(name, "") != value:
        save_dotenv_value(name, value, DOTENV)
        os.environ[name] = value


def choose_mode(arguments: list[str]) -> str:
    """`--funnel` or `--tailnet` if given; else what .env says; else ask once."""
    for mode in MODES:
        if f"--{mode}" in arguments:
            return mode

    kept = os.environ.get(SHARE_ENV, "").strip()
    if kept in MODES:
        return kept

    if not sys.stdin.isatty():
        return "tailnet"

    start.say("Who should reach the console?")
    start.say("  1) Only devices on my tailnet (Tailscale needed; nothing is public)")
    start.say(
        "  2) Anyone I send the link to (Tailscale Funnel; public HTTPS + password)"
    )
    return "funnel" if input("Choose 1 or 2 [1]: ").strip() == "2" else "tailnet"


def funnel(port: int) -> tuple[bool, str]:
    """Publish 127.0.0.1:<port> at this machine's https://…ts.net address.

    Tailscale's output is shown as it comes — the first time it may print a
    link to approve Funnel for the tailnet, and wait for it — and kept, so a
    refusal can be read. Returns whether it worked, and what it said.
    """
    try:
        process = subprocess.Popen(
            [tailscale(), "funnel", "--bg", f"http://127.0.0.1:{port}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except OSError as error:
        return False, str(error)

    said = []
    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="", flush=True)
        said.append(line)
    try:
        process.wait(timeout=300)
    except subprocess.TimeoutExpired:
        process.kill()
        return False, "".join(said)
    return process.returncode == 0, "".join(said)


def allow_this_user() -> bool:
    """Let this user change Tailscale's settings, with the person's yes and sudo.

    By default only root may publish with Funnel; `tailscale set --operator`
    lets one named user do it from then on. Asked once, never assumed.
    """
    import getpass

    user = getpass.getuser()
    if not sys.stdin.isatty():
        return False
    start.say(f"Tailscale lets only root publish with Funnel. Allow '{user}' to, once?")
    start.say(f"  This runs: sudo tailscale set --operator={user}")
    if input("Allow? [Y/n]: ").strip().lower() not in ("", "y", "yes", "e", "evet"):
        return False
    done = subprocess.run(
        ["sudo", tailscale(), "set", f"--operator={user}"], check=False
    )
    return done.returncode == 0


def funnel_off() -> None:
    """Take the public address down, so nothing points at a stopped KIO1."""
    try:
        subprocess.run(
            [tailscale(), "funnel", "--https=443", "off"],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def check_model_machine(endpoint: str) -> None:
    tags = start._get_json(f"{endpoint.rstrip('/')}/api/tags", timeout=5)
    if tags is None:
        start.say(f"The model machine at {endpoint} did not answer. Check that:")
        start.say("  - it is on (tailscale status lists it as online),")
        start.say("  - Ollama there listens on its Tailscale address (docs/deploy.md).")
        start.say("Starting anyway; the console will say when it can plan.")
        return
    names = [str(m.get("name")) for m in tags.get("models", []) if isinstance(m, dict)]
    start.say(f"Models on {endpoint}: {', '.join(names) or 'none yet'}")


def main() -> None:
    os.chdir(ROOT)
    load_dotenv(DOTENV)
    here = tailscale_address()
    dns_name = tailscale_dns_name()
    short_name = dns_name.split(".")[0]

    mode = choose_mode(sys.argv[1:])
    remember(SHARE_ENV, mode)

    if not os.environ.get("KIO1_PROVIDER_ENDPOINT", "").strip():
        # The models may live on this very machine.
        host = "127.0.0.1" if here == MODEL_MACHINE else MODEL_MACHINE
        remember("KIO1_PROVIDER_ENDPOINT", f"http://{host}:{OLLAMA_PORT}")

    if not os.environ.get(PASSWORD_HASH_ENV, "").strip():
        if not sys.stdin.isatty():
            start.say("No console password yet. Run this once by hand to set one:")
            start.say("    python3 run_with_tailscale.py")
            sys.exit(1)
        start.say("First start: choose the password the console will ask for.")
        start.set_password()
        load_dotenv(DOTENV)

    if mode == "funnel":
        if not dns_name:
            start.say("Tailscale did not say this machine's name; Funnel needs it.")
            sys.exit(1)
        # Only Tailscale's Funnel reaches KIO1; nothing listens on the network.
        remember("KIO1_HOST", "127.0.0.1")
        remember("KIO1_CONSOLE_NAME", "")
        remember("KIO1_PUBLIC_URL", f"https://{dns_name}")
    else:
        remember("KIO1_HOST", here)
        remember("KIO1_CONSOLE_NAME", short_name)
        remember("KIO1_PUBLIC_URL", "")

    check_model_machine(os.environ["KIO1_PROVIDER_ENDPOINT"])
    port = start.free_port(os.environ["KIO1_HOST"], 8101)

    if mode == "funnel":
        funnel_off()  # whatever an earlier run left published
        published, said = funnel(port)
        if not published and "access denied" in said.lower() and allow_this_user():
            published, said = funnel(port)
        if not published:
            start.say("Tailscale Funnel could not be switched on. Usually one of:")
            start.say(
                "  - Funnel is not allowed for this machine yet: approve it at the"
            )
            start.say("    link Tailscale printed above, or in the admin console")
            start.say("    (Access controls → Funnel), and turn on HTTPS certificates.")
            start.say("  - This user may not change Tailscale's settings; once, run:")
            start.say("      sudo tailscale set --operator=$USER")
            sys.exit(1)
        start.say(
            f"Public link: https://{dns_name} — anyone with it sees the sign-in page."
        )
    else:
        start.say(
            f"The console is on http://{short_name or here}:{port}, for your tailnet."
        )

    sys.argv = [sys.argv[0], "--no-browser", "--port", str(port)]
    try:
        start.main()
    finally:
        if mode == "funnel":
            funnel_off()


if __name__ == "__main__":
    main()
