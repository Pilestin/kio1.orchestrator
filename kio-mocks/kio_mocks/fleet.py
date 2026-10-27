"""Start the stand-in KIOs, and tell KIO1 where they are.

    python -m kio_mocks                    # every KIO this package has
    python -m kio_mocks --only KIO2 KIO11  # some of them
    python -m kio_mocks --print-config     # the dispatch.agents block for KIO1

Ports follow KIO1's convention: KIO<N> listens on 81NN, so KIO3 is 8103 and
KIO13 is 8113. A KIO whose module is not written yet is simply not started.
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import signal
import threading
from typing import TYPE_CHECKING, Any

from kio_mocks.protocol import serve

if TYPE_CHECKING:
    from http.server import ThreadingHTTPServer

    from kio_mocks.protocol import Kio

ALL_IDS = [f"KIO{number}" for number in range(2, 14)]


def port_of(kio_id: str) -> int:
    """`8100 + N` for `KIO<N>`."""
    return 8100 + int(kio_id.removeprefix("KIO"))


def available() -> dict[str, Kio]:
    """The KIOs whose module exists, by id, in order."""
    found: dict[str, Kio] = {}

    for kio_id in ALL_IDS:
        try:
            module = importlib.import_module(f"kio_mocks.{kio_id.lower()}")
        except ModuleNotFoundError as error:
            if error.name != f"kio_mocks.{kio_id.lower()}":
                raise
            continue
        found[kio_id] = module.KIO

    return found


def registry_entry(kio: Kio, host: str) -> dict[str, Any]:
    """The `dispatch.agents` entry that reaches one stand-in."""
    entry: dict[str, Any] = {
        "description": kio.description,
        "endpoint": f"http://{host}:{port_of(kio.kio_id)}",
        "path": "/jobs" if kio.jobs else "/execute",
        "capabilities": [item.name for item in kio.capabilities],
    }

    if kio.jobs:
        entry["protocol"] = "kio1-jobs"
        entry["poll"] = {"path": "/jobs/{job_id}", "interval_s": 1, "timeout_s": 240}

    return entry


def start(kios: list[Kio], host: str) -> list[ThreadingHTTPServer]:
    """Serve each KIO on its port, in background threads."""
    servers = []

    for kio in kios:
        server = serve(kio, host, port_of(kio.kio_id))
        threading.Thread(
            target=server.serve_forever, name=f"{kio.kio_id}-http", daemon=True
        ).start()
        servers.append(server)
        print(f"  {kio.kio_id:6s} http://{host}:{port_of(kio.kio_id)}  {kio.title}")

    return servers


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run stand-in KIOs that do their job.")
    parser.add_argument(
        "--host", default="127.0.0.1", help="Interface to bind; 0.0.0.0 for containers."
    )
    parser.add_argument(
        "--only", nargs="+", default=[], help="Start only these, such as KIO2 KIO11."
    )
    parser.add_argument(
        "--print-config",
        action="store_true",
        help="Print KIO1's dispatch.agents block and exit.",
    )
    parser.add_argument("--verbose", action="store_true", help="Log every request.")
    arguments = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if arguments.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    kios = available()
    wanted = [kio_id.upper() for kio_id in arguments.only] or list(kios)
    unknown = [kio_id for kio_id in wanted if kio_id not in kios]

    if unknown:
        parser.error(
            f"Not available: {', '.join(unknown)} (have {', '.join(kios) or 'none'})"
        )

    if arguments.print_config:
        host = "127.0.0.1" if arguments.host == "0.0.0.0" else arguments.host
        print(
            json.dumps(
                {kio_id: registry_entry(kios[kio_id], host) for kio_id in wanted},
                indent=4,
            )
        )
        return

    print(f"Starting {len(wanted)} stand-in KIOs:")
    servers = start([kios[kio_id] for kio_id in wanted], arguments.host)
    print("\nPress Ctrl+C to stop.")

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda signum, frame: stop.set())

    try:
        stop.wait()
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        print("Stopped.")
