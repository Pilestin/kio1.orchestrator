"""A stand-in KIO agent for validating the dispatch layer end to end.

The KIO1-KIO10 integration protocol defines the message KIO1 sends but not
the reply an agent returns. This stub implements the reply shape KIO1
expects, so the communication protocol, endpoint configuration, workflow
tracking, and error handling can be validated before any real agent exists.

Run one stub per agent, over HTTP or over NATS:

    python tools/dummy_agent.py --agent-id KIO2 --port 8102
    python tools/dummy_agent.py --agent-id KIO10 --transport nats

Reply shape:

    {
      "workflow_id": "wf-...",
      "step_id": "s1",
      "agent_id": "KIO2",
      "status": "ok" | "error",
      "output": {...},
      "error": null
    }
"""

import argparse
import asyncio
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

logger = logging.getLogger("dummy_agent")

_MAX_REQUEST_BYTES = 1_048_576

# Canned outputs keyed by capability, so a stub reply looks like something a
# real agent would return rather than an echo of the request.
_CAPABILITY_OUTPUTS: dict[str, dict[str, Any]] = {
    "bug_localization": {
        "summary": "Located 1 candidate defect.",
        "findings": [
            {
                "file": "payment/processor.py",
                "line": 142,
                "confidence": 0.81,
                "description": "Unchecked None return before attribute access.",
            }
        ],
    },
    "diagnosis": {
        "summary": "Null dereference reachable from the retry path.",
        "root_cause": "process_refund() assumes lookup_account() never returns None.",
    },
    "fix_recommendation": {
        "summary": "Guard the lookup result before use.",
        "patch_hint": "if account is None: raise AccountNotFound(account_id)",
    },
    "energy_efficiency": {
        "summary": "Edge deployment is feasible with quantization.",
        "estimated_energy_mj": 41.6,
        "recommendations": ["int8 quantization", "batch size 1 on-device"],
    },
    "tinyml": {
        "summary": "Model fits a 512 KB flash budget after pruning.",
        "model_size_kb": 428,
    },
}


class _AgentRequestHandler(BaseHTTPRequestHandler):
    """Handles execution messages for a single simulated agent."""

    agent_id = "KIO0"
    path_prefix = "/execute"
    latency_seconds = 0.0
    fail_capabilities: frozenset[str] = frozenset()

    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        """Answer an execution message posted to the configured path."""
        if self.path.rstrip("/") != self.path_prefix.rstrip("/"):
            self._send_json(404, {"error": f"Unknown path: {self.path}"})
            return

        message = self._read_message()
        if message is None:
            return

        workflow_id = message.get("workflow_id", "")
        step_id = message.get("step_id", "")
        capability = message.get("capability", "")

        logger.info(
            "Received step: workflow_id=%s step_id=%s capability=%s",
            workflow_id,
            step_id,
            capability,
        )

        if self.latency_seconds > 0:
            time.sleep(self.latency_seconds)

        if capability in self.fail_capabilities:
            self._send_json(
                200,
                {
                    "workflow_id": workflow_id,
                    "step_id": step_id,
                    "agent_id": self.agent_id,
                    "status": "error",
                    "output": None,
                    "error": f"Simulated failure for capability {capability!r}",
                },
            )
            return

        self._send_json(
            200,
            {
                "workflow_id": workflow_id,
                "step_id": step_id,
                "agent_id": self.agent_id,
                "status": "ok",
                "output": _build_output(
                    capability,
                    message.get("task", ""),
                    message.get("data", {}),
                ),
                "error": None,
            },
        )

    def do_GET(self) -> None:
        """Answer a liveness probe on /health."""
        if self.path.rstrip("/") == "/health":
            self._send_json(200, {"agent_id": self.agent_id, "status": "ok"})
            return

        self._send_json(404, {"error": f"Unknown path: {self.path}"})

    def log_message(self, format: str, *args: Any) -> None:
        """Route the server's access log through `logging` instead of stderr."""
        logger.debug("%s - %s", self.address_string(), format % args)

    def _read_message(self) -> dict[str, Any] | None:
        """Read and decode the posted execution message.

        Returns:
            The decoded message, or None when a response has already been
            sent because the request was malformed.
        """
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"error": "Invalid Content-Length header"})
            return None

        if content_length <= 0:
            self._send_json(400, {"error": "Empty request body"})
            return None

        if content_length > _MAX_REQUEST_BYTES:
            self._send_json(413, {"error": "Request body too large"})
            return None

        try:
            decoded = json.loads(self.rfile.read(content_length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            self._send_json(400, {"error": f"Invalid JSON body: {error}"})
            return None

        if not isinstance(decoded, dict):
            self._send_json(400, {"error": "Request body must be a JSON object"})
            return None

        return decoded

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        """Write a JSON response with the given status code.

        Args:
            status: HTTP status code.
            payload: Response body, serialized as JSON.

        Returns:
            None.
        """
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _build_output(
    capability: str,
    task: str,
    data: Any = None,
) -> dict[str, Any]:
    """Return a plausible output object for a capability.

    Args:
        capability: Capability requested by the execution message.
        task: The free-text task, echoed so replies are traceable by eye.
        data: The message's `data` field. Any upstream step ids found in it
            are echoed back, so a caller can confirm that results really were
            fed forward rather than assuming it.

    Returns:
        A canned output object, or a generic acknowledgement.
    """
    output = dict(
        _CAPABILITY_OUTPUTS.get(
            capability,
            {"summary": f"Simulated result for capability {capability!r}."},
        )
    )
    output["received_task"] = task

    upstream = data.get("upstream") if isinstance(data, dict) else None

    if isinstance(upstream, dict):
        output["received_upstream"] = sorted(upstream)

    return output


def build_server(
    *,
    agent_id: str,
    host: str,
    port: int,
    path: str = "/execute",
    latency_seconds: float = 0.0,
    fail_capabilities: frozenset[str] = frozenset(),
) -> ThreadingHTTPServer:
    """Create a stub agent server without starting it.

    Args:
        agent_id: Identifier the stub answers as, such as `KIO2`.
        host: Interface to bind. Use `127.0.0.1` for local runs.
        port: Port to bind. Use `0` to let the OS choose a free port.
        path: Request path execution messages are posted to.
        latency_seconds: Artificial delay before replying, for timeout tests.
        fail_capabilities: Capabilities that answer with `status: error`.

    Returns:
        A server ready for `serve_forever()` or `handle_request()`.
    """
    handler = type(
        "_ConfiguredAgentRequestHandler",
        (_AgentRequestHandler,),
        {
            "agent_id": agent_id,
            "path_prefix": path,
            "latency_seconds": latency_seconds,
            "fail_capabilities": fail_capabilities,
        },
    )

    return ThreadingHTTPServer((host, port), handler)


def server_address(server: ThreadingHTTPServer) -> tuple[str, int]:
    """Return the bound host and port of a running stub server.

    Args:
        server: A bound stub server.

    Returns:
        The host as text and the port as an integer. `server_address` is
        typed as accepting bytes for non-IP address families, so the host
        is normalized here rather than at every call site.
    """
    host, port = server.server_address[:2]

    if isinstance(host, (bytes, bytearray)):
        return host.decode("utf-8", errors="replace"), int(port)

    return str(host), int(port)


def main() -> None:
    """Parse arguments and serve a stub agent until interrupted."""
    parser = argparse.ArgumentParser(description="Run a stub KIO agent.")
    parser.add_argument("--agent-id", default="KIO2", help="Agent id to answer as.")
    parser.add_argument("--host", default="127.0.0.1", help="Interface to bind.")
    parser.add_argument("--port", type=int, default=8102, help="Port to bind.")
    parser.add_argument("--path", default="/execute", help="Execution path.")
    parser.add_argument(
        "--latency",
        type=float,
        default=0.0,
        help="Artificial reply delay in seconds.",
    )
    parser.add_argument(
        "--fail-capability",
        action="append",
        default=[],
        help="Capability that should answer with an error. Repeatable.",
    )
    parser.add_argument(
        "--transport",
        choices=("http", "nats"),
        default="http",
        help="How the stub listens for execution messages.",
    )
    parser.add_argument(
        "--nats-server",
        action="append",
        default=[],
        help="NATS server URL. Repeatable. Defaults to nats://localhost:4222.",
    )
    parser.add_argument(
        "--subject",
        default=None,
        help="NATS subject to listen on. Defaults to kio.<agent>.execute.",
    )
    arguments = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if arguments.transport == "nats":
        _serve_nats(arguments)
        return

    server = build_server(
        agent_id=arguments.agent_id,
        host=arguments.host,
        port=arguments.port,
        path=arguments.path,
        latency_seconds=arguments.latency,
        fail_capabilities=frozenset(arguments.fail_capability),
    )

    host, port = server_address(server)
    print(
        f"{arguments.agent_id} stub listening on http://{host}:{port}{arguments.path}"
    )

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping stub agent.")
    finally:
        server.server_close()


def _serve_nats(arguments: argparse.Namespace) -> None:
    """Serve a NATS stub agent until interrupted.

    Args:
        arguments: Parsed command-line arguments.

    Returns:
        None.
    """
    stub = NatsStubAgent(
        agent_id=arguments.agent_id,
        servers=arguments.nats_server or ["nats://localhost:4222"],
        subject=arguments.subject,
        latency_seconds=arguments.latency,
        fail_capabilities=frozenset(arguments.fail_capability),
    )
    stub.start()
    print(f"{arguments.agent_id} stub listening on NATS subject {stub.subject}")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping stub agent.")
    finally:
        stub.stop()


if __name__ == "__main__":
    main()


DEFAULT_SUBJECT_TEMPLATE = "kio.{agent}.execute"


class NatsStubAgent:
    """Answers execution messages over NATS, for exercising that transport.

    The reply is the same object the HTTP handler returns, so a caller cannot
    tell the two apart other than by how the message arrived.
    """

    def __init__(
        self,
        *,
        agent_id: str,
        servers: list[str],
        subject: str | None = None,
        latency_seconds: float = 0.0,
        fail_capabilities: frozenset[str] = frozenset(),
    ) -> None:
        """Store settings without connecting.

        Args:
            agent_id: Identifier the stub answers as.
            servers: NATS server URLs.
            subject: Subject to listen on. Defaults to the shared template.
            latency_seconds: Artificial delay before replying.
            fail_capabilities: Capabilities that answer with `status: error`.
        """
        self.agent_id = agent_id
        self.subject = subject or DEFAULT_SUBJECT_TEMPLATE.format(
            agent=agent_id.lower()
        )
        self._servers = list(servers)
        self._latency_seconds = latency_seconds
        self._fail_capabilities = fail_capabilities
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._connection: Any = None

    def start(self, timeout: float = 15.0) -> None:
        """Connect and begin answering, returning once the subscription is live.

        Args:
            timeout: Seconds to wait for the subscription to be established.

        Raises:
            RuntimeError: If the stub did not become ready in time.
        """
        ready = threading.Event()
        failure: list[BaseException] = []
        loop = asyncio.new_event_loop()

        def run() -> None:
            asyncio.set_event_loop(loop)

            async def subscribe() -> None:
                import nats

                try:
                    self._connection = await nats.connect(servers=self._servers)
                    await self._connection.subscribe(self.subject, cb=self._handle)
                except BaseException as error:  # noqa: BLE001 - reported to start()
                    failure.append(error)
                finally:
                    ready.set()

            loop.create_task(subscribe())
            loop.run_forever()

        thread = threading.Thread(
            target=run, name=f"kio1-stub-{self.agent_id}", daemon=True
        )
        thread.start()

        if not ready.wait(timeout=timeout):
            raise RuntimeError(f"{self.agent_id} stub did not start in time")

        if failure:
            raise RuntimeError(
                f"{self.agent_id} stub could not subscribe: {failure[0]}"
            ) from failure[0]

        self._loop = loop
        self._thread = thread

        logger.info("%s stub listening on NATS subject %s", self.agent_id, self.subject)

    def stop(self) -> None:
        """Drain the connection and stop the event-loop thread."""
        loop, thread, connection = self._loop, self._thread, self._connection
        self._loop = self._thread = self._connection = None

        if loop is None:
            return

        if connection is not None:
            try:
                asyncio.run_coroutine_threadsafe(connection.drain(), loop).result(
                    timeout=10
                )
            except Exception:
                logger.warning("Draining the stub connection failed", exc_info=True)

        loop.call_soon_threadsafe(loop.stop)

        if thread is not None:
            thread.join(timeout=10)

    async def _handle(self, message: Any) -> None:
        """Answer one execution message.

        Args:
            message: The received NATS message.

        Returns:
            None.
        """
        try:
            payload = json.loads(bytes(message.data).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            logger.warning("Discarding a non-JSON message on %s", self.subject)
            return

        if not isinstance(payload, dict):
            logger.warning("Discarding a non-object message on %s", self.subject)
            return

        if self._latency_seconds > 0:
            await asyncio.sleep(self._latency_seconds)

        capability = payload.get("capability", "")
        reply = {
            "workflow_id": payload.get("workflow_id", ""),
            "step_id": payload.get("step_id", ""),
            "agent_id": self.agent_id,
            "status": "ok",
            "output": _build_output(
                capability,
                payload.get("task", ""),
                payload.get("data", {}),
            ),
            "error": None,
        }

        if capability in self._fail_capabilities:
            reply["status"] = "error"
            reply["output"] = None
            reply["error"] = f"Simulated failure for capability {capability!r}"

        if message.reply:
            await message.respond(json.dumps(reply).encode("utf-8"))
