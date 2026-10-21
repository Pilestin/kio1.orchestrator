"""A KIO agent that generates code with the configured model and writes it out.

`tools/dummy_fleet.py` answers with canned results. That is right for proving
the protocol and misleading for anything else: its file list is fiction, and
nothing it claims to produce exists.

This one is real. It asks whatever model `config.json` configures for the
files a task needs, and writes them to disk. Point KIO1 at it in place of a
stubbed KIO7 and a plan that reaches `code_generation` leaves a directory of
code behind.

    python tools/codegen_agent.py --port 8107 --workspace ./generated

It is a demonstration, not KIO7: one model call, no iteration, no compilation
check, no tests run against what it produced. What it demonstrates is that
the orchestrator drives real work when a real agent is on the other end.

Everything the model returns is treated as untrusted input. Paths are
resolved against the workspace and anything escaping it is refused, so a
model that asks to write `../../.ssh/authorized_keys` gets an error instead.
"""

import argparse
import json
import logging
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config_loader import Config, load_config
from core.formatter import parse_response
from providers.provider_client import load_provider

logger = logging.getLogger("codegen_agent")

AGENT_ID = "KIO7"
CAPABILITIES = ("code_generation", "fix_generation")

# Bounds on what one call may produce. A model that misunderstands the task
# can otherwise fill a disk, and a runaway file is unreadable anyway.
MAX_FILES = 25
MAX_FILE_BYTES = 200_000

SYSTEM_PROMPT = """You generate source files for a software task.

Respond with ONLY a JSON object, no prose and no Markdown fences:

{
  "files": [
    {"path": "relative/path.py", "content": "the complete file contents"}
  ],
  "notes": "one or two sentences on what you produced and why"
}

Rules:
- Paths are relative, use forward slashes, and never contain "..".
- `content` is the whole file, ready to save. Do not abbreviate or elide.
- Produce every file the task needs to run, including an entry point.
- Write real, working code. No placeholders, no "TODO" stubs.
- Keep it small: the fewest files that genuinely satisfy the task."""


class GenerationError(Exception):
    """Raised when a task could not be turned into files."""


def resolve_within(root: Path, candidate: str) -> Path:
    """Return where a model-supplied path may be written.

    Args:
        root: The workspace every file must land inside.
        candidate: The path the model asked for.

    Returns:
        The absolute path to write.

    Raises:
        GenerationError: If the path is absolute, escapes the workspace, or
            is empty. The model is not trusted with the filesystem.
    """
    cleaned = (candidate or "").strip().replace("\\", "/").lstrip("/")

    if not cleaned:
        raise GenerationError("a file was returned with no path")

    target = (root / cleaned).resolve()

    if root not in target.parents and target != root:
        raise GenerationError(
            f"{candidate!r} resolves outside the workspace and was refused"
        )

    return target


def generate(task: str, root: Path, config: Config, provider: Any, client: Any) -> dict:
    """Ask the model for files, write them, and report what landed.

    Args:
        task: The step's instruction.
        root: The workspace to write into.
        config: Application configuration, for the provider call.
        provider: The loaded provider module.
        client: The provider's client object.

    Returns:
        The agent's `output` object, naming the files actually written.

    Raises:
        GenerationError: If the model's answer could not be used.
    """
    started = time.perf_counter()
    response = provider.send_request(
        config=config,
        client=client,
        system_prompt=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": task}],
    )
    content = provider.extract_content(response)

    try:
        answer = parse_response(content)
    except ValueError as error:
        raise GenerationError(
            f"the model did not return usable JSON: {error}"
        ) from error

    if not isinstance(answer, dict) or not isinstance(answer.get("files"), list):
        raise GenerationError(
            "the model's answer has no 'files' list; it returned "
            f"{type(answer).__name__}"
        )

    files = answer["files"][:MAX_FILES]
    written: list[dict[str, Any]] = []

    for entry in files:
        if not isinstance(entry, dict):
            continue

        body = entry.get("content")

        if not isinstance(body, str):
            raise GenerationError(
                f"{entry.get('path')!r} came back without string content"
            )

        if len(body.encode("utf-8")) > MAX_FILE_BYTES:
            raise GenerationError(f"{entry.get('path')!r} is implausibly large")

        target = resolve_within(root, entry.get("path", ""))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")

        written.append(
            {
                "path": str(target.relative_to(root)).replace("\\", "/"),
                "lines": len(body.splitlines()),
                "bytes": len(body.encode("utf-8")),
            }
        )
        logger.info("wrote %s (%d lines)", written[-1]["path"], written[-1]["lines"])

    if not written:
        raise GenerationError("the model returned no usable files")

    elapsed_ms = int((time.perf_counter() - started) * 1000)

    return {
        "summary": f"Wrote {len(written)} file(s) into {root} in {elapsed_ms} ms.",
        "verdict": "clean",
        "workspace": str(root),
        "files": written,
        "notes": answer.get("notes", ""),
        "model": config.model,
        # Nothing here compiled it or ran it. Saying so is cheaper than
        # letting a reader assume otherwise from a "clean" verdict.
        "hitl_required": True,
    }


def build_server(
    host: str, port: int, root: Path, config: Config, provider: Any, client: Any
) -> ThreadingHTTPServer:
    """Return a bound server that answers the KIO1-KIO10 protocol."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            path = self.path.rstrip("/")

            if path == "/health":
                self._send(200, {"agent_id": AGENT_ID, "status": "ok"})
            elif path == "/manifest":
                self._send(
                    200,
                    {
                        "agent_id": AGENT_ID,
                        "description": (
                            "Generates source files for a task with "
                            f"{config.model} and writes them to disk."
                        ),
                        "capabilities": list(CAPABILITIES),
                    },
                )
            else:
                self._send(404, {"error": f"Unknown path: {self.path}"})

        def do_POST(self) -> None:
            if self.path.rstrip("/") != "/execute":
                self._send(404, {"error": f"Unknown path: {self.path}"})
                return

            length = int(self.headers.get("Content-Length", "0"))
            message = json.loads(self.rfile.read(length) or b"{}")
            reply = {
                "workflow_id": message.get("workflow_id", ""),
                "step_id": message.get("step_id", ""),
                "agent_id": AGENT_ID,
            }

            if message.get("capability") not in CAPABILITIES:
                self._send(
                    200,
                    {
                        **reply,
                        "status": "error",
                        "output": None,
                        "error": (
                            f"{AGENT_ID} does not do " f"{message.get('capability')!r}"
                        ),
                    },
                )
                return

            task = message.get("task", "")
            logger.info("generating for: %s", task[:100])

            try:
                output = generate(task, root, config, provider, client)
            except GenerationError as error:
                logger.warning("generation failed: %s", error)
                self._send(
                    200,
                    {**reply, "status": "error", "output": None, "error": str(error)},
                )
                return
            except Exception as error:
                logger.exception("the provider call failed")
                self._send(
                    200,
                    {
                        **reply,
                        "status": "error",
                        "output": None,
                        "error": f"{type(error).__name__}: {error}",
                    },
                )
                return

            self._send(200, {**reply, "status": "ok", "output": output, "error": None})

        def _send(self, code: int, body: dict) -> None:
            payload = json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: Any) -> None:
            """Keep the console readable."""

    return ThreadingHTTPServer((host, port), Handler)


def main() -> int:
    """Serve until interrupted."""
    parser = argparse.ArgumentParser(
        description="A KIO7-shaped agent that really generates and writes code."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8107)
    parser.add_argument(
        "--workspace",
        default="./generated",
        help="Where files are written. Created if missing. Nothing is ever "
        "written outside it.",
    )
    arguments = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="  %(name)s: %(message)s")

    root = Path(arguments.workspace).resolve()
    root.mkdir(parents=True, exist_ok=True)

    config = load_config()
    provider = load_provider(config.provider, config.allowed_providers)
    client = provider.create_client(config)

    print(f"  {AGENT_ID} on http://{arguments.host}:{arguments.port}")
    print(f"  model:     {config.provider} / {config.model}")
    print(f"  workspace: {root}")
    print("\n  Ctrl+C to stop.\n")

    server = build_server(
        arguments.host, arguments.port, root, config, provider, client
    )

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
