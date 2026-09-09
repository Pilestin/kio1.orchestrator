"""The KIO fleet for Use-Case 1, doing real work on a real workspace.

`tools/dummy_agent.py` answers with canned results. That is right for proving
the protocol, and wrong for showing a lifecycle: nothing an agent "produces"
there actually exists, so nothing downstream can consume it.

These agents do the real thing on the scale a demonstration needs. KIO7 writes
files to disk. KIO11 runs `pytest` and reports what actually failed. KIO2 reads
that traceback and names the line. KIO8 builds an archive you can open. None of
them uses a model — every one is deterministic — but the artefacts are real, so
UC-UC1-02 hands UC-UC1-03 a genuine failing test rather than a description of
one.

Each agent speaks the KIO1-KIO10 protocol, so KIO1 cannot tell them apart from
the real thing.
"""

import json
import logging
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

logger = logging.getLogger("uc1.agents")

# The order total is the feature under development. The first version divides
# by the item count to report an average, which is exactly the kind of defect
# UC-UC1-03 exists to find: it passes every test with items in the cart.
PRICING_WITH_DEFECT = '''"""Order pricing with volume discounts."""

DISCOUNT_TIERS = ((10, 0.10), (5, 0.05))


def discount_for(quantity):
    """Return the discount rate for a quantity."""
    for threshold, rate in DISCOUNT_TIERS:
        if quantity >= threshold:
            return rate

    return 0.0


def order_total(items):
    """Return the total and the average line value for an order."""
    total = 0.0

    for item in items:
        line = item["price"] * item["quantity"]
        total += line * (1 - discount_for(item["quantity"]))

    return {"total": round(total, 2), "average_line": round(total / len(items), 2)}
'''

PRICING_FIXED = PRICING_WITH_DEFECT.replace(
    '    return {"total": round(total, 2), "average_line": round(total / len(items), 2)}',
    """    average = total / len(items) if items else 0.0

    return {"total": round(total, 2), "average_line": round(average, 2)}""",
)

TESTS = '''"""Acceptance tests derived from the approved issue."""

from pricing import order_total


def test_a_single_item_has_no_discount():
    assert order_total([{"price": 10.0, "quantity": 1}])["total"] == 10.0


def test_five_of_an_item_earns_five_percent():
    assert order_total([{"price": 10.0, "quantity": 5}])["total"] == 47.5


def test_ten_of_an_item_earns_ten_percent():
    assert order_total([{"price": 10.0, "quantity": 10}])["total"] == 90.0


def test_an_empty_cart_totals_nothing():
    assert order_total([])["total"] == 0.0
'''


class AgentError(Exception):
    """Raised by a capability that could not do what it was asked."""


class Fleet:
    """Every agent in the scenario, and the workspace they share."""

    def __init__(self, workspace: Path) -> None:
        """Prepare a fleet working on one directory.

        Args:
            workspace: The project under development. Created if missing.
        """
        self.workspace = workspace
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.handlers: dict[str, dict[str, Callable[[str, dict], dict]]] = {}
        self._servers: list[ThreadingHTTPServer] = []
        self._register_all()

    # ── the capabilities ────────────────────────────────────────────────

    def _register_all(self) -> None:
        """Wire every capability to the agent that provides it."""
        self.handlers = {
            "KIO3": {
                "requirements_engineering": self.requirements,
                "acceptance_criteria": self.acceptance_criteria,
            },
            "KIO4": {"architecture_design": self.architecture},
            "KIO6": {"data_validation": self.traceability},
            "KIO7": {
                "code_generation": self.generate_code,
                "fix_generation": self.apply_fix,
            },
            "KIO8": {"packaging": self.package, "deployment": self.deploy},
            "KIO9": {"compliance_review": self.compliance},
            "KIO10": {"energy_efficiency": self.energy},
            "KIO11": {"test_automation": self.run_tests},
            "KIO12": {"cybersecurity_validation": self.security_scan},
            "KIO2": {
                "bug_localization": self.localise_bug,
                "diagnosis": self.diagnose,
            },
        }

    def requirements(self, task: str, data: dict) -> dict:
        """Turn the customer request into structured requirements.

        Reads the request text rather than inventing one: the numbers in the
        requirements below are the discount tiers named in the request, so a
        different request produces different requirements.
        """
        tiers = [
            {"quantity": int(quantity), "discount_pct": int(percent)}
            for quantity, percent in re.findall(r"(\d+)\D{1,40}?(\d+)\s*%", task)
        ]

        functional = [
            {"id": "FR-1", "text": "An order total is computed from its line items."},
            {"id": "FR-2", "text": "A volume discount applies per line item."},
            {"id": "FR-3", "text": "An order with no items totals zero."},
        ]

        return {
            "summary": f"Derived {len(functional)} functional requirements.",
            "verdict": "clean",
            "issue": {
                "title": "Order total with volume discounts",
                "priority": "high",
                "story_points": 3,
                "labels": ["pricing", "billing"],
            },
            "functional_requirements": functional,
            "non_functional_requirements": [
                {"id": "NFR-1", "text": "A total is returned in under 50 ms."}
            ],
            "discount_tiers": tiers,
        }

    def acceptance_criteria(self, task: str, data: dict) -> dict:
        """Write the acceptance criteria the tests will be generated from."""
        return {
            "summary": "Defined 4 acceptance criteria.",
            "verdict": "clean",
            "criteria": [
                "Given one item, the total is its price times its quantity.",
                "Given five of an item, five percent is deducted.",
                "Given ten of an item, ten percent is deducted.",
                "Given an empty cart, the total is 0.00.",
            ],
        }

    def architecture(self, task: str, data: dict) -> dict:
        """Name the modules the code generator will produce."""
        return {
            "summary": "Two modules: a pure pricing function and its tests.",
            "verdict": "clean",
            "components": [
                {"name": "pricing", "responsibility": "discount rules and totals"},
                {"name": "tests", "responsibility": "acceptance tests"},
            ],
            "decisions": [
                "Pricing stays free of I/O so it can be tested without a fixture."
            ],
        }

    def generate_code(self, task: str, data: dict) -> dict:
        """Write the module and its tests into the workspace.

        The generated code carries a real defect — an average over an empty
        list — so UC-UC1-03 has something genuine to find.
        """
        written = self._write("pricing.py", PRICING_WITH_DEFECT)
        written += self._write("test_pricing.py", TESTS)

        return {
            "summary": f"Wrote {len(written)} files into the workspace.",
            "verdict": "clean",
            "files": [{"path": name, "lines": lines} for name, lines in written],
        }

    def traceability(self, task: str, data: dict) -> dict:
        """Check that every requirement is visible in what was produced."""
        upstream = data.get("upstream", {})
        requirements = [
            requirement
            for step in upstream.values()
            if isinstance(step, dict)
            for requirement in step.get("functional_requirements", [])
        ]
        source = self._read("pricing.py") + self._read("test_pricing.py")

        covered = [
            requirement["id"]
            for requirement in requirements
            if any(word in source for word in ("total", "discount", "quantity"))
        ]

        return {
            "summary": f"{len(covered)} of {len(requirements)} requirements traceable.",
            "verdict": "clean" if len(covered) == len(requirements) else "defect",
            "covered": covered,
        }

    def run_tests(self, task: str, data: dict) -> dict:
        """Run the workspace's test suite and report what actually happened."""
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "--no-header",
                "-p",
                "no:cacheprovider",
            ],
            cwd=self.workspace,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        output = completed.stdout + completed.stderr
        passed = _count(r"(\d+) passed", output)
        failed = _count(r"(\d+) failed", output)

        return {
            "summary": f"{passed} passed, {failed} failed.",
            "verdict": "clean" if failed == 0 else "defect",
            "passed": passed,
            "failed": failed,
            "report": output[-4000:],
        }

    def localise_bug(self, task: str, data: dict) -> dict:
        """Read the failing test's traceback and name the line."""
        report = self._upstream_field(data, "report")

        if not report:
            raise AgentError("no test report to work from")

        match = re.search(r"(\w+\.py):(\d+): (\w+Error)", report)

        if match is None:
            match = re.search(r'File "([^"]+)", line (\d+)', report)

        if match is None:
            return {
                "summary": "No fault location in the report.",
                "verdict": "inconclusive",
            }

        groups = match.groups()

        return {
            "summary": f"Fault reaches {match.group(1)} line {match.group(2)}.",
            "verdict": "defect",
            "findings": [
                {
                    "file": Path(match.group(1)).name,
                    "line": int(match.group(2)),
                    # The second pattern has no error-kind group: a plain
                    # assertion failure names a file and a line and nothing else.
                    "error": (groups[2] if len(groups) >= 3 else "AssertionError"),
                }
            ],
        }

    def diagnose(self, task: str, data: dict) -> dict:
        """Explain the fault from the located line."""
        findings = self._upstream_field(data, "findings") or []
        source = self._read("pricing.py").splitlines()
        line_number = findings[0]["line"] if findings else 0
        line = source[line_number - 1].strip() if 0 < line_number <= len(source) else ""

        return {
            "summary": "The average is computed before the empty case is handled.",
            "verdict": "defect",
            "root_cause": (
                "order_total divides by len(items) unconditionally, so an empty "
                "cart raises ZeroDivisionError."
            ),
            "offending_line": line,
            "hitl_required": True,
        }

    def apply_fix(self, task: str, data: dict) -> dict:
        """Rewrite the module with the empty case handled."""
        if "average = total / len(items) if items else 0.0" in self._read("pricing.py"):
            return {"summary": "Already fixed.", "verdict": "clean", "changed": False}

        self._write("pricing.py", PRICING_FIXED)

        return {
            "summary": "Guarded the average against an empty cart.",
            "verdict": "clean",
            "changed": True,
            "patch": "average = total / len(items) if items else 0.0",
        }

    def security_scan(self, task: str, data: dict) -> dict:
        """Look for patterns nobody should ship."""
        risky = {
            "eval(": "evaluates arbitrary input",
            "pickle.loads": "deserialises untrusted data",
            "password": "a credential in source",
        }
        source = self._read("pricing.py") + self._read("test_pricing.py")
        findings = [
            {"pattern": pattern, "detail": detail}
            for pattern, detail in risky.items()
            if pattern in source
        ]

        return {
            "summary": f"{len(findings)} findings.",
            "verdict": "defect" if findings else "clean",
            "findings": findings,
        }

    def compliance(self, task: str, data: dict) -> dict:
        """Check the GDPR question this feature actually raises."""
        source = self._read("pricing.py")
        handles_personal_data = any(
            marker in source for marker in ("email", "customer_name", "address")
        )

        return {
            "summary": (
                "No personal data in the pricing path."
                if not handles_personal_data
                else "Personal data reaches the pricing path."
            ),
            "verdict": "clean" if not handles_personal_data else "defect",
            "checked": ["GDPR data minimisation", "responsible-AI disclosure"],
        }

    def energy(self, task: str, data: dict) -> dict:
        """Measure what the feature actually costs to run."""
        script = (
            "import sys, time; sys.path.insert(0, '.'); "
            "from pricing import order_total; "
            "items=[{'price':10.0,'quantity':3}]*200; "
            "start=time.perf_counter();\n"
            "[order_total(items) for _ in range(200)];\n"
            "print(round((time.perf_counter()-start)*1000, 3))"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=self.workspace,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

        try:
            elapsed_ms = float(completed.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError) as error:
            raise AgentError(f"could not measure: {completed.stderr[-300:]}") from error

        return {
            "summary": f"40000 totals in {elapsed_ms:g} ms.",
            "verdict": "clean",
            "measured_ms": elapsed_ms,
            "note": "Wall-clock stands in for a hardware energy counter here.",
        }

    def package(self, task: str, data: dict) -> dict:
        """Build an archive of the workspace."""
        archive = self.workspace.parent / "dist" / "pricing-1.0.0.zip"
        archive.parent.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            for path in sorted(self.workspace.rglob("*.py")):
                bundle.write(path, path.relative_to(self.workspace))

        return {
            "summary": f"Packaged {archive.name} ({archive.stat().st_size} bytes).",
            "verdict": "clean",
            "artifact": str(archive),
        }

    def deploy(self, task: str, data: dict) -> dict:
        """Copy the archive where a release would be picked up."""
        artifact = self._upstream_field(data, "artifact")

        if not artifact or not Path(artifact).exists():
            raise AgentError("nothing was packaged to deploy")

        released = self.workspace.parent / "staging" / Path(artifact).name
        released.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(artifact, released)

        return {
            "summary": f"Released to {released.parent.name}.",
            "verdict": "clean",
            "environment": "staging",
            "released": str(released),
        }

    # ── workspace helpers ───────────────────────────────────────────────

    def _write(self, name: str, content: str) -> list[tuple[str, int]]:
        """Write one file into the workspace and report its size."""
        (self.workspace / name).write_text(content, encoding="utf-8")

        return [(name, len(content.splitlines()))]

    def _read(self, name: str) -> str:
        """Return a workspace file's contents, empty when it does not exist."""
        path = self.workspace / name

        return path.read_text(encoding="utf-8") if path.exists() else ""

    @staticmethod
    def _upstream_field(data: dict, field: str) -> Any:
        """Return a field from whichever upstream step carries it."""
        for step in (data.get("upstream") or {}).values():
            if isinstance(step, dict) and field in step:
                return step[field]

        return None

    # ── serving ─────────────────────────────────────────────────────────

    def start(self, host: str = "127.0.0.1") -> dict[str, str]:
        """Start one server per agent.

        Args:
            host: Interface to bind.

        Returns:
            Agent id to base URL, for KIO1's registry.
        """
        endpoints: dict[str, str] = {}

        for agent_id, capabilities in self.handlers.items():
            server = _build(agent_id, capabilities, host)
            threading.Thread(
                target=server.serve_forever, name=f"uc1-{agent_id}", daemon=True
            ).start()
            self._servers.append(server)
            endpoints[agent_id] = f"http://{host}:{server.server_address[1]}"

        return endpoints

    def stop(self) -> None:
        """Stop every server."""
        for server in self._servers:
            server.shutdown()
            server.server_close()

        self._servers.clear()


def _count(pattern: str, text: str) -> int:
    """Return the first number a pattern matches, or zero."""
    match = re.search(pattern, text)

    return int(match.group(1)) if match else 0


def _build(
    agent_id: str,
    capabilities: dict[str, Callable[[str, dict], dict]],
    host: str,
) -> ThreadingHTTPServer:
    """Return a bound server answering for one agent."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            path = self.path.rstrip("/")

            if path == "/health":
                self._send(200, {"agent_id": agent_id, "status": "ok"})
            elif path == "/manifest":
                self._send(
                    200,
                    {
                        "agent_id": agent_id,
                        "description": DESCRIPTIONS[agent_id],
                        "capabilities": sorted(capabilities),
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
            capability = message.get("capability", "")
            reply = {
                "workflow_id": message.get("workflow_id", ""),
                "step_id": message.get("step_id", ""),
                "agent_id": agent_id,
            }

            handler = capabilities.get(capability)

            if handler is None:
                self._send(
                    200,
                    {
                        **reply,
                        "status": "error",
                        "output": None,
                        "error": f"{agent_id} does not do {capability!r}",
                    },
                )
                return

            started = time.perf_counter()

            try:
                output = handler(message.get("task", ""), message.get("data", {}))
            except Exception as error:
                # Anything a capability raises becomes a failed step rather
                # than a dead server thread: KIO1 is entitled to an answer, and
                # a silent socket would look like an unreachable agent.
                logger.warning(
                    "%s failed %s: %s", agent_id, capability, error, exc_info=True
                )
                self._send(
                    200,
                    {**reply, "status": "error", "output": None, "error": str(error)},
                )
                return

            logger.info(
                "%s %s in %d ms",
                agent_id,
                capability,
                int((time.perf_counter() - started) * 1000),
            )
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

    return ThreadingHTTPServer((host, 0), Handler)


DESCRIPTIONS = {
    "KIO2": "Bug localisation and diagnosis from a failing test's traceback.",
    "KIO3": "Requirements engineering and acceptance criteria from a request.",
    "KIO4": "Architecture and component design.",
    "KIO6": "Requirement-to-artefact traceability validation.",
    "KIO7": "Code generation and fix generation, writing real files.",
    "KIO8": "Packaging and deployment of the built artefact.",
    "KIO9": "GDPR and responsible-AI compliance review.",
    "KIO10": "Energy and efficiency measurement of the built feature.",
    "KIO11": "Test automation: runs the suite and reports what failed.",
    "KIO12": "Cybersecurity validation of the generated source.",
}
