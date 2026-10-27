"""The KIO1 job interface (BITNET §3.5), for KIOs whose work outlasts a request.

The real KIO9 reviews for 15–40 seconds and one review at a time, so a step
is posted and polled:

1. `POST /jobs` with a job request — `schema_version` "1.0", the step's ids,
   capability, task and `data` — answered at once with `status: accepted` and
   a `job_id`.
2. `GET /jobs/{job_id}` until the status is final: `success` (the result in
   `output`), `needs_clarification`, or `failure` (`failure_class`,
   `diagnostics`).

What KIO1's `dispatch/job_transport.py` relies on, and so what this keeps:
`accepted` is never a result; a repeated request (same idempotency key, or
same workflow and step) is answered with the job already made; jobs run one
after another, so a queue forms as it does at the real service.
"""

from __future__ import annotations

import hashlib
import queue
import threading
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kio_mocks.protocol import Kio

SCHEMA_VERSION = "1.0"

ACCEPTED = "accepted"
RUNNING = "running"
SUCCESS = "success"
FAILURE = "failure"

# A job may take much longer than a direct answer: that is why it is a job.
JOB_BUDGET_SECONDS = 600.0


class JobBoard:
    """The jobs one KIO has been given, and the worker that does them."""

    def __init__(self, kio: Kio) -> None:
        self._kio = kio
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._queue: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
        threading.Thread(
            target=self._work, name=f"{kio.kio_id}-jobs", daemon=True
        ).start()

    def submit(self, request: dict[str, Any]) -> dict[str, Any]:
        """Take a job request and answer at once with its envelope."""
        job_id = _job_id(request)

        with self._lock:
            existing = self._jobs.get(job_id)
            if existing is not None:
                return dict(existing)

            job = {
                "schema_version": SCHEMA_VERSION,
                "job_id": job_id,
                "status": ACCEPTED,
                "workflow_id": request.get("workflow_id", ""),
                "step_id": request.get("step_id", ""),
                "submitted_at": time.time(),
            }
            self._jobs[job_id] = job

        self._queue.put((job_id, request))
        return dict(job)

    def get(self, job_id: str) -> dict[str, Any] | None:
        """A job's envelope as it stands, or None."""
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job is not None else None

    def _work(self) -> None:
        while True:
            job_id, request = self._queue.get()
            self._update(job_id, status=RUNNING)
            started = time.monotonic()
            reply = self._kio.execute(request, budget=JOB_BUDGET_SECONDS)

            # The real service takes its time; a poller that never sees
            # `running` has not been tested against one.
            remaining = self._kio.job_delay - (time.monotonic() - started)
            if remaining > 0:
                time.sleep(remaining)

            if reply["status"] == "ok":
                self._update(job_id, status=SUCCESS, output=reply["output"])
            else:
                self._update(
                    job_id,
                    status=FAILURE,
                    failure_class="task_failed",
                    diagnostics={"message": reply["error"]},
                )

    def _update(self, job_id: str, **fields: Any) -> None:
        with self._lock:
            self._jobs[job_id].update(fields, updated_at=time.time())


def _job_id(request: dict[str, Any]) -> str:
    """The same request always names the same job."""
    key = (
        request.get("idempotency_key")
        or f"{request.get('workflow_id')}:{request.get('step_id')}"
    )
    return "job-" + hashlib.sha256(str(key).encode("utf-8")).hexdigest()[:16]
