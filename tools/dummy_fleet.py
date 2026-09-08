"""Start a stand-in for every KIO at once.

Only KIO2 exists as a real service today. A plan that spans the development
lifecycle therefore reports almost every step as `skipped`, which is correct
but shows nothing of the orchestration working. This starts one stub per KIO
so a whole plan runs end to end.

    python tools/dummy_fleet.py

Ports follow KIO1's registry convention: KIO<N> listens on 81<NN>, so KIO3 is
8103 and KIO13 is 8113. KIO2 is left out by default because a real KIO2 exists
and would lose the port to a stub; pass `--include KIO2` to stub it anyway.

The replies are canned. Nothing here does real work — the point is to see the
plan execute, the results flow between steps, and the records land.
"""

import argparse
import json
import logging
import signal
import sys
import threading
from pathlib import Path
from types import FrameType
from typing import Any

if __package__ in (None, ""):
    # Run as `python tools/dummy_fleet.py`, sys.path starts at tools/, so the
    # repository root has to be added before its packages can be imported.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.dummy_agent import build_server, server_address

logger = logging.getLogger("dummy_fleet")

# Capability lists match KIO1's system prompt. A stub declares them so a step
# asking for something else is skipped by KIO1 rather than answered wrongly.
FLEET: dict[str, tuple[str, ...]] = {
    "KIO2": ("bug_localization", "diagnosis", "replay", "trace_alignment"),
    "KIO3": ("requirements_engineering", "user_stories", "acceptance_criteria"),
    "KIO4": ("architecture_design", "component_design"),
    "KIO5": ("synthetic_data_generation",),
    "KIO6": ("data_validation", "quality_analysis"),
    "KIO7": ("code_generation", "code_understanding"),
    "KIO8": ("build", "deployment", "devops", "packaging"),
    "KIO9": ("responsible_ai", "compliance_review"),
    "KIO10": ("tinyml", "energy_efficiency"),
    "KIO11": ("test_automation", "validation"),
    "KIO12": ("cybersecurity_validation",),
    "KIO13": ("developer_training", "onboarding"),
}

# KIO2 answers for real; a stub would take its port and silently replace it.
DEFAULT_EXCLUDED = ("KIO2",)


def port_of(agent_id: str) -> int:
    """Return the port an agent listens on.

    Args:
        agent_id: An agent identifier such as `KIO7`.

    Returns:
        `8100 + N` for `KIO<N>`, matching KIO1's registry convention.

    Raises:
        ValueError: If the identifier does not end in a number.
    """
    digits = agent_id.removeprefix("KIO")

    if not digits.isdigit():
        raise ValueError(f"Cannot derive a port from {agent_id!r}")

    return 8100 + int(digits)


def registry_entry(agent_id: str, host: str) -> dict[str, Any]:
    """Return the `dispatch.agents` entry that reaches this stub.

    Args:
        agent_id: The stub's identifier.
        host: The host KIO1 should use to reach it.

    Returns:
        The configuration object to paste into `config.json`.
    """
    return {
        "endpoint": f"http://{host}:{port_of(agent_id)}",
        "path": "/execute",
        "capabilities": list(FLEET[agent_id]),
    }


def start(agent_ids: list[str], host: str) -> list[Any]:
    """Start one stub per agent and return the running servers.

    Args:
        agent_ids: Which agents to stub.
        host: Interface to bind. `0.0.0.0` makes them reachable from a
            container; `127.0.0.1` keeps them on this machine.

    Returns:
        The started servers, for shutting down later.
    """
    servers = []

    for agent_id in agent_ids:
        server = build_server(
            agent_id=agent_id,
            host=host,
            port=port_of(agent_id),
            capabilities=frozenset(FLEET[agent_id]),
        )
        threading.Thread(
            target=server.serve_forever,
            name=f"kio1-stub-{agent_id}",
            daemon=True,
        ).start()
        servers.append(server)

        bound_host, bound_port = server_address(server)
        print(f"  {agent_id:6s} http://{bound_host}:{bound_port}/execute")

    return servers


def main() -> None:
    """Start the fleet and serve until interrupted."""
    parser = argparse.ArgumentParser(description="Run a stub for every KIO.")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Interface to bind. Use 0.0.0.0 to be reachable from a container.",
    )
    parser.add_argument(
        "--include",
        action="append",
        default=[],
        help="Also stub an agent excluded by default, such as KIO2. Repeatable.",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        help="Stub only these agents. Repeatable.",
    )
    parser.add_argument(
        "--print-config",
        action="store_true",
        help="Print the dispatch.agents block for config.json and exit.",
    )
    arguments = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if arguments.only:
        agent_ids = [agent_id.upper() for agent_id in arguments.only]
    else:
        included = {agent_id.upper() for agent_id in arguments.include}
        agent_ids = [
            agent_id
            for agent_id in FLEET
            if agent_id not in DEFAULT_EXCLUDED or agent_id in included
        ]

    unknown = [agent_id for agent_id in agent_ids if agent_id not in FLEET]

    if unknown:
        parser.error(f"Unknown agents: {', '.join(unknown)}")

    if arguments.print_config:
        entries = {
            agent_id: registry_entry(agent_id, arguments.host) for agent_id in agent_ids
        }
        print(json.dumps({"agents": entries}, indent=4))
        return

    print(f"Starting {len(agent_ids)} stub agents:")
    servers = start(agent_ids, arguments.host)

    if "KIO2" not in agent_ids:
        print("\n  KIO2 is not stubbed; run the real service on 8102.")

    print("\nPress Ctrl+C to stop.")

    stop = threading.Event()

    def handle(signum: int, frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGINT, handle)

    try:
        stop.wait()
    finally:
        print("\nStopping.")
        for server in servers:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    main()
