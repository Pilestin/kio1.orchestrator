"""Screenshot console pages with headless Chrome, and report page errors.

For checking a UI change by looking at it, in both themes and at more than
one width, before calling it done:

    python tools/console_screenshots.py --out shots \\
        run:light:1366 targets:dark:1280 "settings:light:1366:document.getElementById('review-provider').scrollIntoView()"

Each shot is PAGE:THEME[:WIDTH[:SCRIPT]]. PAGE is a console route (run,
targets, settings, help) or a path starting with "/" such as
/static/mockups/chat.html. SCRIPT is JavaScript run after the page settles —
to click or fill something — and may be async.

Needs Chrome, a running KIO1 (start.py), and its token in .env or
data/registry-token. `--force-dark-mode`-style flags were unreliable; this
drives Chrome over the DevTools protocol instead, which emulates the colour
scheme exactly and waits for the page rather than guessing.
"""

import argparse
import asyncio
import base64
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

import websockets

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.dotenv import parse_dotenv

CHROME_PATHS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
)
PORT = 9333


def find_chrome() -> str:
    for path in CHROME_PATHS:
        if os.path.exists(path):
            return path
    sys.exit("Chrome or Edge was not found; pass --chrome.")


def find_token() -> str:
    env = ROOT / ".env"
    if env.exists():
        token = parse_dotenv(env.read_text(encoding="utf-8")).get("KIO1_REGISTRY_TOKEN")
        if token:
            return token
    stored = ROOT / "data" / "registry-token"
    return stored.read_text(encoding="utf-8").strip() if stored.exists() else ""


def _read(request: "str | urllib.request.Request") -> bytes:
    """Fetch a DevTools endpoint; run in a thread from async code."""
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.read()


class Page:
    """One DevTools connection, collecting errors while it waits."""

    def __init__(self, ws: Any) -> None:
        self.ws = ws
        self.counter = 0
        self.errors: list[str] = []

    def note(self, message: dict) -> None:
        method, params = message.get("method"), message.get("params", {})
        if method == "Runtime.exceptionThrown":
            details = params.get("exceptionDetails", {})
            self.errors.append(
                "exception: "
                + str(
                    details.get("exception", {}).get("description")
                    or details.get("text")
                )
            )
        elif (
            method == "Log.entryAdded"
            and params.get("entry", {}).get("level") == "error"
        ):
            entry = params["entry"]
            self.errors.append(f"log: {entry.get('text')} {entry.get('url', '')}")

    async def call(self, method: str, **params: object) -> dict:
        self.counter += 1
        await self.ws.send(
            json.dumps({"id": self.counter, "method": method, "params": params})
        )
        while True:
            message = json.loads(await self.ws.recv())
            if message.get("id") == self.counter:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return message.get("result", {})
            self.note(message)

    async def wait(self, seconds: float) -> None:
        loop = asyncio.get_running_loop()
        end = loop.time() + seconds
        while (remaining := end - loop.time()) > 0:
            try:
                self.note(json.loads(await asyncio.wait_for(self.ws.recv(), remaining)))
            except asyncio.TimeoutError:
                break


async def shoot(
    base: str, token: str, out: Path, shots: list[str], settle: float
) -> None:
    for shot in shots:
        page_name, theme, *rest = shot.split(":", 3)
        width = int(rest[0]) if rest and rest[0] else 1366
        script = rest[1] if len(rest) > 1 else ""

        if page_name.startswith("/"):
            url = f"{base}{page_name}{'&' if '?' in page_name else '?'}theme={theme}"
        else:
            url = f"{base}/#/{page_name}&token={token}"

        target = json.loads(
            await asyncio.to_thread(
                _read,
                urllib.request.Request(
                    f"http://127.0.0.1:{PORT}/json/new?about:blank", method="PUT"
                ),
            )
        )
        async with websockets.connect(
            target["webSocketDebuggerUrl"], max_size=50_000_000
        ) as ws:
            page = Page(ws)
            await page.call(
                "Emulation.setDeviceMetricsOverride",
                width=width,
                height=1000,
                deviceScaleFactor=1,
                mobile=width < 700,
            )
            await page.call(
                "Emulation.setEmulatedMedia",
                features=[
                    {"name": "prefers-color-scheme", "value": theme},
                    {"name": "prefers-reduced-motion", "value": "reduce"},
                ],
            )
            for domain in ("Page", "Runtime", "Log"):
                await page.call(f"{domain}.enable")
            await page.call("Page.navigate", url=url)
            await page.wait(settle)

            if script:
                await page.call(
                    "Runtime.evaluate", expression=script, awaitPromise=True
                )
                await page.wait(settle)

            image = await page.call("Page.captureScreenshot", format="png")
            name = f"{page_name.strip('/').replace('/', '_') or 'page'}-{theme}-{width}.png"
            path = out / name
            path.write_bytes(base64.b64decode(image["data"]))
            print(path, "| errors:", page.errors or "none")

        await asyncio.to_thread(
            _read, f"http://127.0.0.1:{PORT}/json/close/{target['id']}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("shots", nargs="+", help="PAGE:THEME[:WIDTH[:SCRIPT]]")
    parser.add_argument(
        "--base", default="http://127.0.0.1:8101", help="KIO1's address"
    )
    parser.add_argument("--out", default="shots", help="directory for the PNGs")
    parser.add_argument(
        "--settle", type=float, default=2.5, help="seconds to let a page settle"
    )
    parser.add_argument("--chrome", default="", help="path to Chrome or Edge")
    arguments = parser.parse_args()

    out = Path(arguments.out)
    out.mkdir(parents=True, exist_ok=True)
    chrome = subprocess.Popen(
        [
            arguments.chrome or find_chrome(),
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            f"--remote-debugging-port={PORT}",
            f"--user-data-dir={out.resolve() / '.profile'}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(40):
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{PORT}/json/version", timeout=1
                )
                break
            except OSError:
                time.sleep(0.25)
        asyncio.run(
            shoot(
                arguments.base.rstrip("/"),
                find_token(),
                out,
                arguments.shots,
                arguments.settle,
            )
        )
    finally:
        chrome.terminate()


if __name__ == "__main__":
    main()
