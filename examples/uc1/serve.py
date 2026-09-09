"""Serve Use-Case 1 through the Workflow API, so it can be driven from the console.

    python examples/uc1/serve.py

`run.py` drives the dispatch layer directly, which is the right shape for a
test. This instead stands up everything a person would use: the UC1 fleet, a
Session Manager, the Workflow API, and the console at
`http://127.0.0.1:8000/`.

Requests are planned by a **scripted planner** rather than a model, so the
demonstration works without one installed. It matches on what you type; the
phrases it recognises are listed in `--help` and in the console's own hint.
Pass `--provider` to plan with whatever `config.json` configures instead.
"""

import argparse
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.session_logger import configure_console_encoding
from examples.uc1 import scenario
from examples.uc1.agents import Fleet
from examples.uc1.run import build_dispatch

logger = logging.getLogger("uc1.serve")

DEFAULT_TOKEN = "uc1-demo-token"

# What to type in the console, and which scenario it runs. Order matters: the
# first phrase found in the request wins, so the more specific ones come first.
SCRIPT: tuple[tuple[tuple[str, ...], Any, str], ...] = (
    (
        ("release", "deploy", "ship", "staging"),
        scenario.uc1_04,
        "UC-UC1-04 — final checks, packaging, release",
    ),
    (
        ("bug", "failing", "fix", "broken", "diagnose"),
        scenario.uc1_03,
        "UC-UC1-03 — localise, diagnose, patch, verify",
    ),
    (
        ("code", "implement", "generate", "build the"),
        scenario.uc1_02,
        "UC-UC1-02 — architecture, code, tests, checks",
    ),
    (
        ("requirement", "issue", "story", "discount", "customer"),
        scenario.uc1_01,
        "UC-UC1-01 — requirements and a draft issue",
    ),
)


class ScriptedProvider:
    """Answers with one of the Use-Case 1 plans instead of calling a model.

    It implements the two methods the workflow runner uses, so the runner
    cannot tell it apart from a real provider. Everything downstream of the
    plan — routing, gates, failover, selection — is the real thing.
    """

    @staticmethod
    def plan_for(request: str) -> dict[str, Any]:
        """Return the scenario a request asks for.

        Args:
            request: What the user typed.

        Returns:
            The matching plan, falling back to UC-UC1-01 because that is where
            the lifecycle starts.
        """
        lowered = request.lower()

        for phrases, build, _ in SCRIPT:
            if any(phrase in lowered for phrase in phrases):
                return build()

        return scenario.uc1_01()

    def send_request(self, **kwargs: Any) -> dict[str, Any]:
        """Return the plan for the last message, in the provider reply shape."""
        messages = kwargs.get("messages") or [{}]
        request = str(messages[-1].get("content", ""))
        plan = self.plan_for(request)

        logger.info("Planned %s for %r", plan["workflow_id"], request[:60])

        return {"content": json.dumps(plan)}

    @staticmethod
    def extract_content(response: dict[str, Any]) -> str:
        """Return the plan text from a reply."""
        return str(response["content"])


def main() -> int:
    """Start everything and serve until interrupted."""
    # The banner and the argument help carry arrows and em dashes, which a
    # default Windows console cannot encode.
    configure_console_encoding()

    parser = argparse.ArgumentParser(
        description="Serve Use-Case 1 through the Workflow API and console.",
        epilog=(
            "Phrases the scripted planner recognises: "
            + "; ".join(
                f"{'/'.join(phrases[:2])} → {label.split(' — ')[0]}"
                for phrases, _, label in reversed(SCRIPT)
            )
        ),
    )
    parser.add_argument("--port", type=int, default=8000, help="Console port.")
    parser.add_argument(
        "--token",
        default=os.environ.get("KIO1_REGISTRY_TOKEN", DEFAULT_TOKEN),
        help="Registry token the console asks for.",
    )
    parser.add_argument(
        "--provider",
        action="store_true",
        help="Plan with the provider config.json configures, not the script.",
    )
    parser.add_argument(
        "--workspace", help="Where the project is built. Temporary by default."
    )
    arguments = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="  %(name)s: %(message)s")
    logging.getLogger("uc1.agents").setLevel(logging.INFO)

    os.environ["KIO1_REGISTRY_TOKEN"] = arguments.token
    os.environ.setdefault("KIO1_SESSION_MANAGER_URL", "http://127.0.0.1:8801")

    import uvicorn

    from core.config_loader import load_config
    from core.session_client import SessionClient
    from dispatch.directory import AgentDirectory
    from dispatch.scoreboard import Scoreboard
    from dispatch.transport_factory import build_transport
    from providers.provider_client import load_provider
    from services.session_manager.app import app as session_app
    from services.workflow_api import app as api_module
    from services.workflow_api.runner import WorkflowRunner

    root = (
        Path(arguments.workspace).resolve()
        if arguments.workspace
        else Path(tempfile.mkdtemp(prefix="kio1-uc1-serve-"))
    )
    workspace = root / "project"
    fleet = Fleet(workspace)
    endpoints = fleet.start()

    threading.Thread(
        target=lambda: uvicorn.run(
            session_app, host="127.0.0.1", port=8801, log_level="error"
        ),
        daemon=True,
    ).start()

    def build_state() -> Any:
        """Assemble the service state around the running fleet."""
        config = load_config()
        config.dispatch = build_dispatch(endpoints, workspace)

        directory = AgentDirectory(
            config.dispatch.registry,
            discovery=config.dispatch.discovery,
            health=config.dispatch.health,
        )
        directory.start()
        scoreboard = Scoreboard(config.dispatch.selection)

        if arguments.provider:
            provider: Any = load_provider(config.provider, config.allowed_providers)
            provider_client = provider.create_client(config)
        else:
            provider, provider_client = ScriptedProvider(), None

        recorder = SessionClient(os.environ["KIO1_SESSION_MANAGER_URL"])

        return api_module._State(
            runner=WorkflowRunner(
                config=config,
                prompt_template="You are KIO1.",
                provider=provider,
                provider_client=provider_client,
                recorder=recorder,
                transport=build_transport(config.dispatch),
                directory=directory,
                scoreboard=scoreboard,
            ),
            session_client=recorder,
            directory=directory,
            scoreboard=scoreboard,
        )

    # The service builds its own state at startup from config.json. Replacing
    # that is how this example points it at the fleet without a second config
    # file, and it keeps the real lifespan — including the shutdown that stops
    # the directory's background pass.
    api_module._build_state = build_state  # type: ignore[assignment]

    print(f"\n  Console:   http://127.0.0.1:{arguments.port}/")
    print(f"  Token:     {arguments.token}")
    print(f"  Workspace: {workspace}")
    print(
        f"  Planning:  {'config.json provider' if arguments.provider else 'scripted'}"
    )
    print("\n  Try, in order:")

    for phrases, _, label in reversed(SCRIPT):
        print(f'    "{phrases[0]}…"  →  {label}')

    print("\n  Ctrl+C to stop.\n")

    try:
        uvicorn.run(
            api_module.app, host="127.0.0.1", port=arguments.port, log_level="error"
        )
    except KeyboardInterrupt:
        pass
    finally:
        fleet.stop()

        if not arguments.workspace:
            shutil.rmtree(root, ignore_errors=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
