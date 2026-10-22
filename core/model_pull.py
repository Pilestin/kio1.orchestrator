"""Downloading a local model from the console.

`ollama pull` in a terminal is a step a non-developer can trip on: which
terminal, which command, is it done yet. This runs the same download through
Ollama's API in a background thread and keeps its progress, so the console
can show a bar and say when the model is ready.
"""

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any

logger = logging.getLogger(__name__)

STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"


@dataclass
class PullProgress:
    """Where one download stands.

    Attributes:
        model: The model being pulled.
        state: `running`, `done`, or `failed`.
        status: Ollama's own word for the current phase, such as
            "pulling manifest" or "verifying sha256 digest".
        completed: Bytes downloaded of the current layer.
        total: Bytes in the current layer, 0 when not known yet.
        error: Why it failed.
        started_at: When it began.
    """

    model: str
    state: str = STATE_RUNNING
    status: str = "starting"
    completed: int = 0
    total: int = 0
    error: str = ""
    started_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Return the progress as JSON-ready data, with a percentage."""
        percent = round(100 * self.completed / self.total, 1) if self.total else None
        return {**asdict(self), "percent": percent}


class PullManager:
    """Runs model downloads in the background and remembers how they went."""

    def __init__(self) -> None:
        """Start with no downloads."""
        self._pulls: dict[str, PullProgress] = {}
        self._lock = threading.Lock()

    def start(self, endpoint: str, model: str) -> PullProgress:
        """Begin downloading a model, unless it is already downloading.

        Args:
            endpoint: Ollama's base URL.
            model: The model to pull.

        Returns:
            Its progress, new or already under way.
        """
        with self._lock:
            current = self._pulls.get(model)

            if current is not None and current.state == STATE_RUNNING:
                return current

            progress = PullProgress(model=model, started_at=time.time())
            self._pulls[model] = progress

        threading.Thread(
            target=self._pull,
            args=(endpoint.rstrip("/"), progress),
            name=f"kio1-pull-{model}",
            daemon=True,
        ).start()

        return progress

    def status(self) -> list[dict[str, Any]]:
        """Return every download this process has started, newest first."""
        with self._lock:
            pulls = sorted(
                self._pulls.values(), key=lambda p: p.started_at, reverse=True
            )

        return [pull.to_dict() for pull in pulls]

    @staticmethod
    def _pull(endpoint: str, progress: PullProgress) -> None:
        """Stream `/api/pull`, recording each line of progress."""
        body = json.dumps({"model": progress.model, "stream": True}).encode("utf-8")
        request = urllib.request.Request(
            f"{endpoint}/api/pull",
            data=body,
            headers={"Content-Type": "application/json"},
        )

        try:
            # No overall timeout: a few gigabytes on a slow line take as
            # long as they take. The socket timeout bounds a stalled stream.
            with urllib.request.urlopen(request, timeout=120) as response:
                for line in response:
                    if not line.strip():
                        continue

                    update = json.loads(line.decode("utf-8"))

                    if "error" in update:
                        raise RuntimeError(update["error"])

                    progress.status = str(update.get("status", progress.status))

                    if "total" in update:
                        progress.total = int(update.get("total") or 0)
                        progress.completed = int(update.get("completed") or 0)

            progress.state = STATE_DONE
            progress.status = "ready"
            logger.info("Model pulled: %s", progress.model)
        except (urllib.error.URLError, OSError, ValueError, RuntimeError) as error:
            progress.state = STATE_FAILED
            progress.error = str(getattr(error, "reason", error))
            logger.warning("Model pull failed: %s: %s", progress.model, progress.error)
