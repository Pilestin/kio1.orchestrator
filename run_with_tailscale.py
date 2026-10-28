"""Start KIO1 on this server and publish it at a public HTTPS link.

    python3 run_with_tailscale.py

No options, no questions. It:

1. Points the planner at Ollama on the model machine (`MODEL_MACHINE`, over
   Tailscale), unless `.env` already names another `KIO1_PROVIDER_ENDPOINT`.
2. Makes sure there is a console password. If there is none it makes one,
   prints it, and keeps it in `data/console-password` (readable by this user
   only) — `.env` holds only its hash. `python3 start.py --set-password`
   chooses another.
3. Publishes KIO1 with Tailscale Funnel at `https://<machine>.<tailnet>.ts.net`,
   a link anyone can open, with a certificate Tailscale manages. KIO1 itself
   listens on 127.0.0.1 only. If Tailscale lets only root publish, it makes
   this user Tailscale's operator first (sudo may ask for your password).
4. Starts KIO1, and takes the link down again when KIO1 stops.

Anyone with the link reaches the sign-in page and nothing else; the session
cookie only travels over HTTPS. See docs/deploy.md.
"""

from __future__ import annotations

import getpass
import json
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import start
from core.console_auth import PASSWORD_HASH_ENV, hash_password
from core.dotenv import load_dotenv, save_dotenv_value

# The shared machine that serves the models, by its Tailscale address.
MODEL_MACHINE = "100.71.36.18"
OLLAMA_PORT = 11434
DOTENV = ROOT / ".env"
PASSWORD_FILE = ROOT / "data" / "console-password"


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


def tailscale_status() -> dict:
    """`tailscale status --json`, or exit saying Tailscale is not connected."""
    try:
        found = subprocess.run(
            [tailscale(), "status", "--json"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        status = json.loads(found.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        status = {}
    if not isinstance(status, dict) or not status.get("Self", {}).get("DNSName"):
        start.say("Tailscale is not connected. Run:  sudo tailscale up")
        sys.exit(1)
    return status


def remember(name: str, value: str) -> None:
    """Keep a value in .env and in this process."""
    if os.environ.get(name, "") != value:
        save_dotenv_value(name, value, DOTENV)
        os.environ[name] = value


def ensure_password() -> None:
    """Make a console password if there is none, and say where it is."""
    if os.environ.get(PASSWORD_HASH_ENV, "").strip():
        return
    password = secrets.token_urlsafe(12)
    remember(PASSWORD_HASH_ENV, hash_password(password))
    PASSWORD_FILE.parent.mkdir(exist_ok=True)
    PASSWORD_FILE.write_text(password + "\n", encoding="utf-8")
    if os.name == "posix":
        os.chmod(PASSWORD_FILE, 0o600)
    start.say(f"Console password: {password}")
    start.say(f"  (kept in {PASSWORD_FILE.relative_to(ROOT)}; change it with")
    start.say("   python3 start.py --set-password)")


def funnel(port: int) -> tuple[bool, str]:
    """Publish 127.0.0.1:<port> at this machine's https://…ts.net address.

    Tailscale's output is shown as it comes — the first time it may print a
    link to approve Funnel for the tailnet, and wait for it — and kept, so a
    refusal can be read.
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
    if process.stdout is not None:
        for line in process.stdout:
            print(line, end="", flush=True)
            said.append(line)
    try:
        process.wait(timeout=300)
    except subprocess.TimeoutExpired:
        process.kill()
        return False, "".join(said)
    return process.returncode == 0, "".join(said)


def make_operator() -> bool:
    """Let this user change Tailscale's settings, as `tailscale funnel` needs.

    Tailscale lets only root publish until a user is named its operator.
    """
    start.say("Letting this user publish with Tailscale (sudo may ask for a password).")
    done = subprocess.run(
        ["sudo", tailscale(), "set", f"--operator={getpass.getuser()}"], check=False
    )
    return done.returncode == 0


def funnel_off() -> None:
    """Take the public link down, so it never points at a stopped KIO1."""
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
        start.say(f"The model machine at {endpoint} did not answer; check that it is")
        start.say(
            "on and that Ollama listens on its Tailscale address. Starting anyway."
        )
        return
    names = [str(m.get("name")) for m in tags.get("models", []) if isinstance(m, dict)]
    start.say(f"Models on {endpoint}: {', '.join(names) or 'none yet'}")


def main() -> None:
    os.chdir(ROOT)
    load_dotenv(DOTENV)
    status = tailscale_status()
    dns_name = str(status["Self"]["DNSName"]).rstrip(".")
    here = next((ip for ip in status["Self"].get("TailscaleIPs", []) if "." in ip), "")

    if not os.environ.get("KIO1_PROVIDER_ENDPOINT", "").strip():
        # The models may live on this very machine.
        host = "127.0.0.1" if here == MODEL_MACHINE else MODEL_MACHINE
        remember("KIO1_PROVIDER_ENDPOINT", f"http://{host}:{OLLAMA_PORT}")

    ensure_password()

    # Only Funnel reaches KIO1; nothing listens on the network itself.
    public_url = f"https://{dns_name}"
    remember("KIO1_HOST", "127.0.0.1")
    remember("KIO1_CONSOLE_NAME", "")
    remember("KIO1_PUBLIC_URL", public_url)

    check_model_machine(os.environ["KIO1_PROVIDER_ENDPOINT"])
    port = start.free_port("127.0.0.1", 8101)

    funnel_off()  # whatever an earlier run left published
    published, said = funnel(port)
    if not published and "access denied" in said.lower() and make_operator():
        published, said = funnel(port)
    if not published:
        start.say("Tailscale Funnel could not be switched on. If Tailscale printed a")
        start.say("link above, open it to allow Funnel for this tailnet, then run")
        start.say("this again.")
        sys.exit(1)

    start.say(f"Public link: {public_url}")
    sys.argv = [sys.argv[0], "--no-browser", "--port", str(port)]
    try:
        start.main()
    finally:
        funnel_off()


if __name__ == "__main__":
    main()
