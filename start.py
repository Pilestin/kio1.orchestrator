"""Start KIO1 with one command, from a fresh checkout or an existing one.

    python start.py            # or double-click start.cmd / run ./start.sh

Everything a first run needs, in order, each step skipped when already done:

1. A virtual environment in `.venv`, with the runtime requirements installed.
   They are reinstalled only when `requirements.txt` changes.
2. `config.json`, created from `config.example.json`.
3. An administration token, generated once and kept in `data/`, so the
   console can manage agents without anyone choosing or typing a secret.
4. The model provider: for Ollama, the server is started if it is installed
   but not running, and a missing model is named with the command to get it.
5. The web service, on 8101 (the next free port past the KIOs' if it is
   taken), and a browser opened
   on it with the token already handed over.

Only the standard library is used here, because this runs before any
dependency is installed.
"""

import argparse
import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from core.console_auth import PASSWORD_HASH_ENV, hash_password
from core.dotenv import load_dotenv, save_dotenv_value
from core.endpoints import exposed, is_local

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
REQUIREMENTS = ROOT / "requirements.txt"
CONFIG = ROOT / "config.json"
EXAMPLE_CONFIG = ROOT / "config.example.json"
DATA = ROOT / "data"
TOKEN_FILE = DATA / "registry-token"
STAMP = VENV / ".kio1-requirements.sha256"

MINIMUM_PYTHON = (3, 10)
OLLAMA_DOWNLOAD = "https://ollama.com/download"


def say(message: str) -> None:
    """Print one progress line."""
    print(f"[kio1] {message}", flush=True)


def venv_python() -> Path:
    """Return the virtual environment's interpreter for this platform."""
    if os.name == "nt":
        return VENV / "Scripts" / "python.exe"

    return VENV / "bin" / "python"


def ensure_python() -> None:
    """Refuse to continue on a Python too old to run KIO1."""
    if sys.version_info < MINIMUM_PYTHON:
        wanted = ".".join(str(part) for part in MINIMUM_PYTHON)
        sys.exit(
            f"KIO1 needs Python {wanted} or newer; this is {sys.version.split()[0]}. "
            "Install a newer one from https://www.python.org/downloads/"
        )


def ensure_environment(skip_install: bool) -> Path:
    """Create the virtual environment and install requirements when needed.

    Args:
        skip_install: Use the environment as it is, even if requirements
            changed.

    Returns:
        The interpreter to run KIO1 with.
    """
    python = venv_python()

    if not python.exists():
        say("Creating a virtual environment in .venv (first run only)...")
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)

    if skip_install:
        return python

    wanted = hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()
    installed = STAMP.read_text(encoding="utf-8").strip() if STAMP.exists() else ""

    if wanted != installed:
        say("Installing dependencies (this takes a minute the first time)...")
        subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "-q",
                "-r",
                str(REQUIREMENTS),
            ],
            check=True,
        )
        STAMP.write_text(wanted, encoding="utf-8")

    return python


def ensure_config() -> dict:
    """Create `config.json` from the example when missing, then read it."""
    if not CONFIG.exists():
        shutil.copyfile(EXAMPLE_CONFIG, CONFIG)
        say("Created config.json from config.example.json.")

    return json.loads(CONFIG.read_text(encoding="utf-8"))


def ensure_token() -> str:
    """Return the administration token, creating one on first run.

    An explicit `KIO1_REGISTRY_TOKEN` wins. Otherwise a random token is kept
    in `data/registry-token`, which is not tracked by git.
    """
    configured = os.environ.get("KIO1_REGISTRY_TOKEN", "").strip()

    if configured:
        return configured

    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text(encoding="utf-8").strip()

    DATA.mkdir(exist_ok=True)
    token = secrets.token_urlsafe(24)
    TOKEN_FILE.write_text(token, encoding="utf-8")
    say(f"Generated an administration token in {TOKEN_FILE.relative_to(ROOT)}.")

    return token


def _get_json(url: str, timeout: float) -> dict | None:
    """Return a decoded JSON object from a URL, or None when unreachable."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            decoded = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None

    return decoded if isinstance(decoded, dict) else None


def check_ollama(config: dict) -> None:
    """Make sure Ollama answers and has the configured model.

    Problems are reported rather than fatal: the console opens either way and
    shows what is wrong, which is a better place to fix it than a terminal.
    """
    # KIO1_PROVIDER_ENDPOINT wins, as it does for the service itself: on a
    # server it points at the model machine, often over Tailscale.
    endpoint = (
        os.environ.get("KIO1_PROVIDER_ENDPOINT", "").strip()
        or str(
            config.get("provider_options", {}).get("endpoint", "http://127.0.0.1:11434")
        )
    ).rstrip("/")
    model = str(config.get("model", ""))

    if exposed(endpoint):
        say(f"Warning: {endpoint} is on the internet without HTTPS — every prompt")
        say("crosses it readable. Reach it over Tailscale or a VPN instead.")

    tags = _get_json(f"{endpoint}/api/tags", timeout=5)

    # Ollama on another machine is not ours to start.
    if tags is None and not is_local(endpoint):
        say(f"The model server at {endpoint} did not answer.")
        say("If it is on your tailnet: is Tailscale up here (tailscale status), and")
        say("is Ollama there listening on its Tailscale address? Starting anyway.")
        return

    if tags is None:
        executable = shutil.which("ollama")

        if executable is None:
            say(f"Ollama is not installed. Get it from {OLLAMA_DOWNLOAD},")
            say("or choose another provider in config.json. Starting anyway.")
            return

        say("Starting Ollama...")
        subprocess.Popen(
            [executable, "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        for _ in range(30):
            time.sleep(0.5)
            tags = _get_json(f"{endpoint}/api/tags", timeout=2)

            if tags is not None:
                break
        else:
            say(f"Ollama did not answer at {endpoint}. Starting anyway.")
            return

    names = {
        entry.get("name") for entry in tags.get("models", []) if isinstance(entry, dict)
    }
    # `ministral-3:3b` and `ministral-3:3b-latest` style names both count.
    if model and model not in names and f"{model}:latest" not in names:
        say(f"The model '{model}' is not downloaded yet. In another terminal run:")
        say(f"    ollama pull {model}")
        say("Or pick an installed model on the console's Settings tab.")


# KIO<N> listens on 81NN — KIO1 on 8101, KIO2 on 8102, … KIO13 on 8113 — so a
# busy 8101 must not push KIO1 onto a port a KIO is about to take.
KIO_PORTS = range(8102, 8114)


def free_port(host: str, start: int) -> int:
    """Return the first port from `start` nothing is listening on, past the KIOs' own."""
    for port in range(start, start + 50):
        if port in KIO_PORTS:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if probe.connect_ex((host, port)) != 0:
                return port

    sys.exit(f"No free port between {start} and {start + 49}.")


def wait_until_up(url: str, process: subprocess.Popen, seconds: float) -> bool:
    """Wait for the service to answer its health check."""
    deadline = time.time() + seconds

    while time.time() < deadline:
        if process.poll() is not None:
            return False

        if _get_json(f"{url}/health", timeout=1) is not None:
            return True

        time.sleep(0.5)

    return False


LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def set_password() -> None:
    """Ask for the console password twice and keep only its hash in `.env`."""
    import getpass

    first = getpass.getpass("New console password (12+ characters): ")
    if len(first) < 12:
        say("Too short: use at least 12 characters.")
        sys.exit(1)
    if getpass.getpass("Again: ") != first:
        say("The two did not match.")
        sys.exit(1)

    save_dotenv_value(PASSWORD_HASH_ENV, hash_password(first), ROOT / ".env")
    protect_dotenv()
    say("Saved. The console asks for this password from the next start.")


def protect_dotenv() -> None:
    """Make `.env` readable by its owner only, where the system allows it.

    It holds API keys and the password's hash; on a shared server another
    account should not read it.
    """
    if os.name == "posix" and (ROOT / ".env").exists():
        os.chmod(ROOT / ".env", 0o600)


def main() -> None:
    """Prepare everything and run KIO1 until interrupted."""
    parser = argparse.ArgumentParser(description="Start the KIO1 console.")
    parser.add_argument("--port", type=int, default=8101, help="first port to try")
    parser.add_argument(
        "--host",
        default="",
        help="address to listen on (default: KIO1_HOST in .env, else 127.0.0.1)",
    )
    parser.add_argument(
        "--no-browser", action="store_true", help="do not open a browser"
    )
    parser.add_argument(
        "--set-password",
        action="store_true",
        help="set the console password (needed to listen beyond this machine)",
    )
    parser.add_argument(
        "--skip-install",
        action="store_true",
        help="use .venv as it is, without checking requirements",
    )
    arguments = parser.parse_args()

    os.chdir(ROOT)

    if arguments.set_password:
        set_password()
        return

    ensure_python()
    # Before the token is decided, so a KIO1_REGISTRY_TOKEN in .env is the
    # one used rather than a generated one.
    load_dotenv(ROOT / ".env")
    # Chosen once and kept in .env — run_with_tailscale.py writes it — so a
    # plain `python3 start.py` opens where it opened last time.
    arguments.host = (
        arguments.host or os.environ.get("KIO1_HOST", "").strip() or "127.0.0.1"
    )
    python = ensure_environment(arguments.skip_install)
    config = ensure_config()
    token = ensure_token()
    protect_dotenv()

    # Beyond this machine, a KIO1 without a password runs workflows — paid
    # model calls, commands on the server — for anyone who finds the port.
    if (
        arguments.host not in LOOPBACK
        and not os.environ.get(PASSWORD_HASH_ENV, "").strip()
    ):
        say(f"Not starting on {arguments.host}: there is no console password.")
        say("Set one with:  python start.py --set-password")
        sys.exit(1)

    if config.get("provider") == "ollama":
        check_ollama(config)

    port = free_port(arguments.host, arguments.port)
    url = f"http://{arguments.host}:{port}"
    environment = {**os.environ, "KIO1_REGISTRY_TOKEN": token}

    say(f"Starting KIO1 on {url} ...")
    process = subprocess.Popen(
        [
            str(python),
            "-m",
            "uvicorn",
            "app:app",
            "--host",
            arguments.host,
            "--port",
            str(port),
        ],
        env=environment,
    )

    try:
        if not wait_until_up(url, process, seconds=120):
            say("KIO1 did not start; the lines above say why.")
            process.wait()
            sys.exit(process.returncode or 1)

        # The name people type — a Tailscale machine name, say — rather than
        # the address bound to; both reach the same place.
        name = os.environ.get("KIO1_CONSOLE_NAME", "").strip()
        console = f"http://{name}:{port}" if name else url
        # Behind a password the token stays out of the address: it would sit
        # in the terminal's scrollback and the browser's history, and signing
        # in already opens the administration pages.
        if not os.environ.get(PASSWORD_HASH_ENV, "").strip():
            console = f"{console}/#token={token}"
        say("")
        say(f"KIO1 is running. Console: {console}")
        say("Press Ctrl+C here to stop it.")

        if not arguments.no_browser:
            webbrowser.open(console)

        process.wait()
    except KeyboardInterrupt:
        say("Stopping...")
        process.terminate()

        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


if __name__ == "__main__":
    main()
