"""Starting and stopping the stand-in KIOs from the console.

The stand-ins in `kio-mocks/` are a program of their own, and KIO1 keeps it
that way: it never imports them, it runs `python -m kio_mocks` in that
folder and reads what the program prints — the same as a person in a
terminal would. Which KIOs came up is taken from the lines it prints on
start (`  KIO2   http://127.0.0.1:8102  …`), so nothing about them has to be
known here.

One fleet at a time, owned by this process: it stops when KIO1 stops, so a
closed console leaves nothing running on 81NN.
"""

import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FOLDER = ROOT / "kio-mocks"
LOG = ROOT / "data" / "kio-mocks.log"

_STARTED_LINE = re.compile(r"^\s+(KIO\d+)\s+(http://\S+)")

# Long enough for the fleet to bind its ports or fail to.
_SETTLE_SECONDS = 1.5


class StandInError(Exception):
    """The fleet could not be started; the message says why."""


class StandIns:
    """The stand-in fleet this KIO1 started, if any."""

    def __init__(
        self, folder: Path = FOLDER, command: list[str] | None = None, log: Path = LOG
    ) -> None:
        """Set where the fleet lives and how it is started.

        Args:
            folder: The `kio-mocks` folder.
            command: What starts it; `python -m kio_mocks` when omitted.
                Tests replace it with something that does not bind ports.
            log: Where its output goes.
        """
        self._folder = folder
        self._command = command or [sys.executable, "-u", "-m", "kio_mocks"]
        self._log = log
        self._process: subprocess.Popen[bytes] | None = None
        self._started_at = 0.0
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        """Whether this installation has the stand-ins at all."""
        return (self._folder / "kio_mocks").is_dir()

    @property
    def sample(self) -> Path:
        """The small project with one real bug the stand-ins are tried on."""
        return self._folder / "sample"

    def running(self) -> bool:
        with self._lock:
            return self._process is not None and self._process.poll() is None

    def start(self) -> dict[str, Any]:
        """Start the fleet, or say why it did not come up.

        Raises:
            StandInError: If there are no stand-ins here, or the program
                exited at once — usually because something else already
                holds the ports.
        """
        if not self.available:
            raise StandInError(f"There are no stand-in KIOs in {self._folder}.")

        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return self._status()

            self._log.parent.mkdir(parents=True, exist_ok=True)
            with self._log.open("wb") as output:
                self._process = subprocess.Popen(
                    self._command,
                    cwd=self._folder,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                )
            self._started_at = time.time()

        time.sleep(_SETTLE_SECONDS)

        if not self.running():
            tail = " ".join(self._tail().splitlines()[-2:]) or "no output"
            raise StandInError(f"The stand-ins stopped at once: {tail}")

        return self.status()

    def stop(self) -> dict[str, Any]:
        """Stop the fleet, if this KIO1 started one."""
        with self._lock:
            process, self._process = self._process, None

        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

        return self.status()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return self._status()

    def _status(self) -> dict[str, Any]:
        alive = self._process is not None and self._process.poll() is None
        kios = []
        if alive:
            for line in self._tail().splitlines():
                found = _STARTED_LINE.match(line)
                if found:
                    kios.append(
                        {"agent_id": found.group(1), "endpoint": found.group(2)}
                    )

        return {
            "available": self.available,
            "running": alive,
            "pid": self._process.pid if alive and self._process else None,
            "started_at": self._started_at if alive else None,
            "kios": kios,
            "sample": str(self.sample) if self.sample.is_dir() else None,
        }

    def _tail(self) -> str:
        try:
            return self._log.read_text(encoding="utf-8", errors="replace")[-4000:]
        except OSError:
            return ""
