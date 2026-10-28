"""Start KIO1 on this server, on the tailnet, planning with the shared model machine.

    python3 run_with_tailscale.py

No options. The first time, it asks for what it cannot know — a console
password — and keeps the answers in `.env`, so every later start is the same
command, and so is a plain `python3 start.py`. What it does:

1. Finds this machine's Tailscale address. The console listens there and
   nowhere else, so only devices signed in to the tailnet can reach it; the
   internet sees no open port.
2. Points the planner at Ollama on the model machine (`MODEL_MACHINE`, over
   Tailscale), unless `.env` already names another `KIO1_PROVIDER_ENDPOINT`.
3. Makes sure a console password is set, asking for one if not.
4. Checks that the model machine answers, then starts KIO1.

See docs/deploy.md for the model machine's side and for running this as a
service.
"""

from __future__ import annotations

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


def tailscale_address() -> str:
    """This machine's Tailscale IPv4 address, or exit saying how to get one."""
    executable = shutil.which("tailscale")
    if executable is None:
        start.say("Tailscale is not installed here. Install it:")
        start.say(
            "    curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up"
        )
        sys.exit(1)

    try:
        found = subprocess.run(
            [executable, "ip", "-4"],
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


def remember(name: str, value: str) -> None:
    """Keep a value in .env and in this process."""
    save_dotenv_value(name, value, DOTENV)
    os.environ[name] = value


def main() -> None:
    os.chdir(ROOT)
    load_dotenv(DOTENV)
    here = tailscale_address()

    # Listen on the tailnet only; a plain start.py will do the same from now on.
    if os.environ.get("KIO1_HOST", "").strip() != here:
        remember("KIO1_HOST", here)

    if not os.environ.get("KIO1_PROVIDER_ENDPOINT", "").strip():
        # The models may live on this very machine.
        host = "127.0.0.1" if here == MODEL_MACHINE else MODEL_MACHINE
        remember("KIO1_PROVIDER_ENDPOINT", f"http://{host}:{OLLAMA_PORT}")
    endpoint = os.environ["KIO1_PROVIDER_ENDPOINT"]

    if not os.environ.get(PASSWORD_HASH_ENV, "").strip():
        if not sys.stdin.isatty():
            start.say("No console password yet. Run this once by hand to set one:")
            start.say("    python3 run_with_tailscale.py")
            sys.exit(1)
        start.say("First start: choose the password the console will ask for.")
        start.set_password()
        load_dotenv(DOTENV)

    tags = start._get_json(f"{endpoint.rstrip('/')}/api/tags", timeout=5)
    if tags is None:
        start.say(f"The model machine at {endpoint} did not answer. Check that:")
        start.say("  - it is on (tailscale status lists it as online),")
        start.say("  - Ollama there listens on its Tailscale address (docs/deploy.md).")
        start.say("Starting anyway; the console will say when it can plan.")
    else:
        names = [
            str(m.get("name")) for m in tags.get("models", []) if isinstance(m, dict)
        ]
        start.say(f"Models on {endpoint}: {', '.join(names) or 'none yet'}")

    start.say(f"The console will be on {here}, for devices on your tailnet.")
    sys.argv = [sys.argv[0], "--no-browser"]
    start.main()


if __name__ == "__main__":
    main()
