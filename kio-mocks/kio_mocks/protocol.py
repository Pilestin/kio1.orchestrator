"""The KIO1–KIO10 protocol, as a stand-in KIO speaks it.

A stand-in is only useful if KIO1 cannot tell it from the real service, so
this follows the real KIO2 (`AI4SWENG-KIO2/src/kio2/kio1_protocol.py`) where
the shared protocol document leaves things open:

- **Always HTTP 200 for a task that failed.** A non-2xx status means the
  service is broken; a failed task is `status: "error"` inside a 200.
- **`status` is always set.** KIO1 reads a missing one as `ok`.
- **`workflow_id` and `step_id` are echoed**, which is how KIO1 matches a
  reply to its step.
- **A capability the KIO does not offer is a clear error**, naming what it
  does offer — never a plausible-looking answer to the wrong question.
- **Work finishes inside a time budget** below KIO1's dispatch timeout, so
  the reply arrives as an error rather than not at all.

Served on `GET /health`, `/manifest`, `/tasks`, `/schema` and `POST /execute`;
a KIO that answers through the job interface also serves `POST /jobs` and
`GET /jobs/{id}` (see `jobs.py`).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from kio_mocks.errors import KioError
from kio_mocks.workspace import Workspace, as_object

__all__ = ["Capability", "Kio", "KioError", "Request", "serve"]

logger = logging.getLogger("kio_mocks")

# Every reply says what it is, so nobody mistakes a stand-in's work for the
# real KIO's: a run's history keeps the output, and the output keeps this.
SIMULATOR = "kio-mocks"

_MAX_REQUEST_BYTES = 4 * 1024 * 1024

# Below KIO1's default 60-second dispatch timeout, with room for the reply.
DEFAULT_BUDGET_SECONDS = float(os.environ.get("KIO_MOCKS_BUDGET", "45"))

VERDICTS = ("clean", "defect", "inconclusive")


@dataclass(frozen=True)
class Request:
    """One execution message, read once so every capability sees the same thing.

    Attributes:
        workflow_id: Echoed in the reply.
        step_id: Echoed in the reply.
        capability: What is being asked for.
        task: The free-text instruction.
        data: The message's `data`, as sent.
        workspace: The code under analysis, from `data.repository`.
        upstream: Outputs of the steps this one depends on, by step id.
        arguments: Named inputs, from `data.arguments`.
    """

    workflow_id: str
    step_id: str
    capability: str
    task: str
    data: dict[str, Any]
    workspace: Workspace
    upstream: dict[str, Any]
    arguments: dict[str, Any]

    @classmethod
    def from_message(cls, message: dict[str, Any]) -> Request:
        """Read an execution message; tolerant, as KIO2 is, of missing fields."""
        data = as_object(message.get("data"))
        upstream = as_object(data.get("upstream"))
        arguments = as_object(data.get("arguments"))

        return cls(
            workflow_id=str(message.get("workflow_id", "")),
            step_id=str(message.get("step_id", "")),
            capability=str(message.get("capability", "")),
            task=str(message.get("task", "")),
            data=data,
            workspace=Workspace.from_data(data),
            upstream=upstream,
            arguments=arguments,
        )

    def upstream_outputs(self) -> list[dict[str, Any]]:
        """The dependencies' outputs, in the order the plan listed them."""
        return [output for output in self.upstream.values() if isinstance(output, dict)]

    def find_upstream(self, key: str) -> Any:
        """The first dependency output carrying `key`, or None."""
        for output in self.upstream_outputs():
            if key in output:
                return output[key]
        return None


Handler = Callable[[Request], dict[str, Any]]


@dataclass
class Capability:
    """One thing a KIO can be asked to do."""

    name: str
    describe: str
    handler: Handler


@dataclass
class Kio:
    """A stand-in KIO: who it is, what it does, and how it answers.

    Attributes:
        kio_id: `KIO2` … `KIO13`.
        title: Its name in the DoA, for the manifest.
        description: What it does, in the words the planner reads.
        capabilities: What it can be asked for.
        unsupported: Capabilities people expect of it that it deliberately
            does not offer, each with the reason — KIO2 does not fix bugs.
        jobs: Whether it answers through the job interface, as KIO9 does.
        job_delay: Seconds a job takes at least, so a caller polls as it
            would against the real service.
        budget: Seconds a direct answer may take.
    """

    kio_id: str
    title: str
    description: str
    capabilities: list[Capability]
    unsupported: dict[str, str] = field(default_factory=dict)
    jobs: bool = False
    job_delay: float = 0.0
    budget: float = DEFAULT_BUDGET_SECONDS

    def capability(self, name: str) -> Capability | None:
        """The capability called `name`, or None."""
        return next((item for item in self.capabilities if item.name == name), None)

    def manifest(self) -> dict[str, Any]:
        """What KIO1's discovery reads from `/manifest`."""
        return {
            "agent_id": self.kio_id,
            "description": self.description,
            "path": "/jobs" if self.jobs else "/execute",
            "capabilities": [item.name for item in self.capabilities],
        }

    def tasks(self) -> dict[str, Any]:
        """What `/tasks` lists, in the shape the real KIO2 uses."""
        return {
            "agent_id": self.kio_id,
            "service": self.title,
            "capabilities": [item.name for item in self.capabilities],
            "describe": {item.name: item.describe for item in self.capabilities},
            "unsupported_capabilities": sorted(self.unsupported),
            "job_contract": (
                {"submit": "POST /jobs", "poll": "GET /jobs/{job_id}"}
                if self.jobs
                else None
            ),
            "simulator": SIMULATOR,
        }

    def execute(
        self, message: dict[str, Any], budget: float | None = None
    ) -> dict[str, Any]:
        """Answer one execution message, never raising: the reply says what happened.

        Args:
            message: The decoded execution message.
            budget: Seconds the work may take; the KIO's own when omitted.

        Returns:
            A reply in the KIO1–KIO10 shape.
        """
        request = Request.from_message(message)
        reply = {
            "workflow_id": request.workflow_id,
            "step_id": request.step_id,
            "agent_id": self.kio_id,
        }
        capability = self.capability(request.capability)

        if capability is None:
            reason = self.unsupported.get(request.capability) or (
                f"{self.kio_id} does not offer {request.capability or 'that'!r}; it offers "
                + ", ".join(item.name for item in self.capabilities)
                + "."
            )
            return {**reply, "status": "error", "output": None, "error": reason}

        try:
            output = _within(
                budget if budget is not None else self.budget,
                capability.handler,
                request,
            )
        except KioError as error:
            return {**reply, "status": "error", "output": None, "error": str(error)}
        except TimeoutError:
            return {
                **reply,
                "status": "error",
                "output": None,
                "error": f"{self.kio_id} did not finish {request.capability} within its time budget.",
            }
        except (
            Exception
        ) as error:  # a bug in a stand-in is a failed step, not a dead server
            logger.exception("%s %s failed", self.kio_id, request.capability)
            return {
                **reply,
                "status": "error",
                "output": None,
                "error": f"{self.kio_id} failed: {type(error).__name__}: {error}",
            }

        output.setdefault("simulator", SIMULATOR)

        if output.get("verdict") not in (None, *VERDICTS):
            output["verdict"] = "inconclusive"

        return {**reply, "status": "ok", "output": output, "error": None}


def _within(budget: float, handler: Handler, request: Request) -> dict[str, Any]:
    """Run a handler, giving up once its budget is spent.

    The work goes on in its thread after giving up — Python cannot stop a
    thread — but whatever it runs as a subprocess has its own timeout, and
    the caller has its answer.
    """
    result: dict[str, Any] = {}

    def run() -> None:
        # Anything, so it can be re-raised in the caller's thread.
        try:
            result["output"] = handler(request)
        except BaseException as error:  # noqa: BLE001
            result["error"] = error

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(budget)

    if worker.is_alive():
        raise TimeoutError()
    if "error" in result:
        raise result["error"]

    output = result.get("output")
    if not isinstance(output, dict):
        raise KioError("The stand-in produced no output object.")

    return output


# ── HTTP ─────────────────────────────────────────────────────────────────────


def make_handler(kio: Kio, board: Any = None) -> type[BaseHTTPRequestHandler]:
    """The request handler for one KIO, optionally with its job board."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            path = self.path.split("?")[0].rstrip("/")

            if path == "/health":
                self._send(
                    200,
                    {"agent_id": kio.kio_id, "status": "ok", "simulator": SIMULATOR},
                )
            elif path == "/manifest":
                self._send(200, kio.manifest())
            elif path == "/tasks":
                self._send(200, kio.tasks())
            elif path == "/schema":
                self._send(200, SCHEMA)
            elif board is not None and path.startswith("/jobs/"):
                job = board.get(path[len("/jobs/") :])
                if job is None:
                    self._send(
                        404,
                        {"error": {"code": "unknown_job", "message": "No such job."}},
                    )
                else:
                    self._send(200, job)
            else:
                self._send(404, {"error": f"Unknown path: {self.path}"})

        def do_POST(self) -> None:
            path = self.path.split("?")[0].rstrip("/")
            message = self._read()

            if message is None:
                return

            if path == "/execute":
                self._send(200, kio.execute(message))
            elif board is not None and path == "/jobs":
                self._send(200, board.submit(message))
            else:
                self._send(404, {"error": f"Unknown path: {self.path}"})

        def log_message(self, format: str, *args: Any) -> None:
            logger.debug("%s %s", kio.kio_id, format % args)

        def _read(self) -> dict[str, Any] | None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1

            if length <= 0 or length > _MAX_REQUEST_BYTES:
                self._send(
                    400, {"error": "A JSON body of reasonable size is required."}
                )
                return None

            try:
                decoded = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                self._send(400, {"error": f"Invalid JSON body: {error}"})
                return None

            if not isinstance(decoded, dict):
                self._send(400, {"error": "The body must be a JSON object."})
                return None

            return decoded

        def _send(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def serve(kio: Kio, host: str, port: int) -> ThreadingHTTPServer:
    """A server for one KIO, bound and not yet serving."""
    from kio_mocks.jobs import JobBoard

    board = JobBoard(kio) if kio.jobs else None
    return ThreadingHTTPServer((host, port), make_handler(kio, board))


# The contract, published as the real KIO2 publishes its own on `/schema`.
SCHEMA: dict[str, Any] = {
    "request": {
        "type": "object",
        "required": ["workflow_id", "step_id", "capability", "task"],
        "properties": {
            "workflow_id": {"type": "string"},
            "step_id": {"type": "string"},
            "agent_id": {"type": "string"},
            "capability": {"type": "string"},
            "endpoint": {"type": "string"},
            "task": {"type": "string"},
            "data": {"type": "object"},
        },
    },
    "response": {
        "type": "object",
        "required": ["workflow_id", "step_id", "agent_id", "status"],
        "properties": {
            "status": {"enum": ["ok", "error"]},
            "output": {"type": ["object", "null"]},
            "error": {"type": ["string", "null"]},
        },
    },
}
