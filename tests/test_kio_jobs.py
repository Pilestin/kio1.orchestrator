"""Tests for KIO agents on the KIO1 job interface (`"protocol": "kio1-jobs"`).

KIO9 is the case they are written for: a compliance gate whose post is
answered with `accepted` and a job id, whose review arrives at a poll path
15–40 seconds later, and whose verdict is `output.gate`. The property that
matters most is that nothing short of a final `pass` reads as a pass.
"""

import json
import re
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar

import pytest

from core.config_loader import DispatchConfig
from dispatch.agent_registry import AgentRegistry, KioJobSpec, load_agent_registry
from dispatch.agent_transport import AgentTransportError
from dispatch.job_transport import job_request
from dispatch.transport_factory import build_transport
from dispatch.workflow_executor import (
    STATUS_ERROR,
    STATUS_OK,
    StepResult,
    execute_workflow,
)
from dispatch.workflow_planner import ExecutionMessage, parse_plan

REVIEW_DATA = {
    "contract": {"id": "ctr-uc2-release-01", "version": 1},
    "artifacts": [{"artifact_id": "a1", "kind": "document_instance", "content": {}}],
}


class JobHandler(BaseHTTPRequestHandler):
    """A stub KIO on the job interface: `polls` answers of `accepted`, then `final`."""

    polls: ClassVar[int] = 1
    final: ClassVar[dict[str, Any]] = {}
    refuse: ClassVar[dict[str, Any] | None] = None
    submitted: ClassVar[list[dict[str, Any]]] = []
    headers_seen: ClassVar[list[dict[str, str]]] = []
    jobs: ClassVar[dict[tuple[str, str], str]] = {}
    seen: ClassVar[dict[str, int]] = {}

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        cls = type(self)
        cls.submitted.append(body)
        cls.headers_seen.append(dict(self.headers))

        if cls.refuse is not None:
            self._send(200, cls.refuse)
            return

        # The same workflow_id + step_id is the same job, as KIO9 does it.
        key = (body["workflow_id"], body["step_id"])
        job_id = cls.jobs.setdefault(key, f"job-{len(cls.jobs) + 1}")
        self._send(202, self._envelope(body, job_id, "accepted"))

    def do_GET(self) -> None:
        cls = type(self)
        job_id = self.path.rsplit("/", 1)[-1]

        if job_id not in cls.jobs.values():
            self._send(404, {"error": "job_not_found"})
            return

        cls.seen[job_id] = cls.seen.get(job_id, 0) + 1
        if cls.seen[job_id] <= cls.polls:
            self._send(200, {"job_id": job_id, "status": "accepted"})
            return

        self._send(200, {"job_id": job_id, **cls.final})

    @staticmethod
    def _envelope(body: dict[str, Any], job_id: str, status: str) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "workflow_id": body["workflow_id"],
            "step_id": body["step_id"],
            "job_id": job_id,
            "status": status,
        }

    def _send(self, code: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args: Any) -> None:
        """Keep the test output clean."""


@pytest.fixture
def kio9() -> Iterator[str]:
    """Run a stub KIO9 and yield its base URL."""
    JobHandler.polls = 1
    JobHandler.final = {
        "status": "success",
        "output": {"gate": "pass", "verdict": "conformant"},
    }
    JobHandler.refuse = None
    JobHandler.submitted = []
    JobHandler.headers_seen = []
    JobHandler.jobs = {}
    JobHandler.seen = {}

    server = ThreadingHTTPServer(("127.0.0.1", 0), JobHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    try:
        yield f"http://127.0.0.1:{int(server.server_address[1])}"
    finally:
        server.shutdown()
        server.server_close()


def job_entry(endpoint: str, **overrides: Any) -> dict[str, Any]:
    """Return a KIO9 entry on the job interface, polling fast enough for a test."""
    entry: dict[str, Any] = {
        "endpoint": endpoint,
        "path": "/jobs",
        "protocol": "kio1-jobs",
        "poll": {"path": "/jobs/{job_id}", "interval_s": 0.02, "timeout_s": 5},
        "capabilities": ["compliance_review", "contract_specification"],
    }
    entry.update(overrides)
    return entry


def review(registry: AgentRegistry, data: dict[str, Any] | None = None) -> StepResult:
    """Run one compliance_review step and return its result."""
    plan = parse_plan(
        {
            "workflow_id": "wf-kio9",
            "execution_mode": "sequential",
            "steps": [
                {
                    "step_id": "s1",
                    "agent_id": "KIO9",
                    "capability": "compliance_review",
                    "task": "Review the report",
                }
            ],
        }
    )
    result = execute_workflow(
        plan,
        registry,
        build_transport(DispatchConfig(enabled=True, registry=registry)),
        timeout=1,
        max_parallel_steps=1,
        workspace_data=data if data is not None else dict(REVIEW_DATA),
    )
    return result.results[0]


def message(data: dict[str, Any], **overrides: Any) -> ExecutionMessage:
    fields: dict[str, Any] = {
        "workflow_id": "wf-kio9",
        "step_id": "s1",
        "agent_id": "KIO9",
        "capability": "compliance_review",
        "endpoint": "http://kio9",
        "task": "Review the report",
        "data": data,
    }
    fields.update(overrides)
    return ExecutionMessage(**fields)


# ── the entry ────────────────────────────────────────────────────────────────


def test_a_job_entry_is_read_with_its_poll_settings() -> None:
    agent = load_agent_registry({"KIO9": job_entry("http://kio9")}).resolve("KIO9")

    assert agent is not None
    assert agent.jobs == KioJobSpec(
        poll_path="/jobs/{job_id}", interval=0.02, timeout=5
    )


def test_an_entry_without_a_protocol_is_not_a_job_entry() -> None:
    entry = {"endpoint": "http://kio2", "capabilities": ["diagnosis"]}
    agent = load_agent_registry({"KIO2": entry}).resolve("KIO2")

    assert agent is not None
    assert agent.jobs is None


def test_a_poll_path_without_the_job_id_is_refused() -> None:
    entry = job_entry("http://kio9", poll={"path": "/jobs/latest"})

    with pytest.raises(ValueError, match=r"\{job_id\}"):
        load_agent_registry({"KIO9": entry})


def test_poll_settings_on_an_entry_that_is_not_a_job_entry_are_refused() -> None:
    entry = {"endpoint": "http://kio2", "poll": {"timeout_s": 10}}

    with pytest.raises(ValueError, match="kio1-jobs"):
        load_agent_registry({"KIO2": entry})


def test_an_unknown_protocol_is_refused() -> None:
    with pytest.raises(ValueError, match="protocol must be one of"):
        load_agent_registry({"KIO9": job_entry("http://kio9", protocol="grpc")})


# ── the request ──────────────────────────────────────────────────────────────


def test_the_request_is_a_job_request_not_the_execution_message(kio9: str) -> None:
    review(load_agent_registry({"KIO9": job_entry(kio9)}))
    sent = JobHandler.submitted[0]

    assert sent["schema_version"] == "1.0"
    assert sent["capability"] == "compliance_review"
    assert sent["data"]["contract"] == REVIEW_DATA["contract"]
    assert "agent_id" not in sent
    assert "endpoint" not in sent


def test_job_ids_use_only_the_characters_the_interface_allows() -> None:
    spec = KioJobSpec()
    request = job_request(message({}, workflow_id="wf İzmir/01", step_id="s 1"), spec)

    for name in ("workflow_id", "step_id"):
        assert re.fullmatch(r"[A-Za-z0-9._-]{1,64}", request[name]), request[name]


def test_the_same_step_with_the_same_content_is_the_same_job() -> None:
    first = job_request(message(dict(REVIEW_DATA)), KioJobSpec())
    second = job_request(message(dict(REVIEW_DATA)), KioJobSpec())

    assert first["step_id"] == second["step_id"]


def test_a_reused_workflow_id_with_another_report_is_a_new_job() -> None:
    # A planner reuses ids like wf-weather01 across runs. KIO9 answers a
    # repeated workflow_id + step_id with the job it has, so the content has to
    # be part of the id, or the second report gets the first one's review.
    other = {**REVIEW_DATA, "contract": {"id": "ctr-uc2-release-01", "version": 2}}
    first = job_request(message(dict(REVIEW_DATA)), KioJobSpec())
    second = job_request(message(other), KioJobSpec())

    assert first["step_id"] != second["step_id"]


def test_a_repeated_dispatch_does_not_create_a_second_job(kio9: str) -> None:
    registry = load_agent_registry({"KIO9": job_entry(kio9)})
    review(registry)
    review(registry)

    assert len(JobHandler.submitted) == 2
    assert len(JobHandler.jobs) == 1


def test_the_data_template_takes_the_payload_from_an_earlier_step(kio9: str) -> None:
    # The planner cannot write a contract id or a report hash. A step that
    # produces the payload comes first, and the template passes it through.
    producer = {
        "type": "cli",
        "command": [
            sys.executable,
            "-c",
            f"import json; print(json.dumps({{'data': {REVIEW_DATA!r}}}))",
        ],
        "capabilities": ["report_export"],
    }
    registry = load_agent_registry(
        {"Export": producer, "KIO9": job_entry(kio9, data="{data.upstream.s1.data}")}
    )
    plan = parse_plan(
        {
            "workflow_id": "wf-kio9",
            "execution_mode": "sequential",
            "steps": [
                {
                    "step_id": "s1",
                    "agent_id": "Export",
                    "capability": "report_export",
                    "task": "Export",
                },
                {
                    "step_id": "s2",
                    "agent_id": "KIO9",
                    "capability": "compliance_review",
                    "task": "Review the report",
                    "depends_on": ["s1"],
                },
            ],
        }
    )
    execute_workflow(
        plan,
        registry,
        build_transport(DispatchConfig(enabled=True, registry=registry)),
        timeout=10,
        max_parallel_steps=1,
    )

    assert JobHandler.submitted[0]["data"] == REVIEW_DATA


def test_a_data_template_with_nothing_to_fill_it_fails_the_step_by_saying_so(
    kio9: str,
) -> None:
    step = review(
        load_agent_registry({"KIO9": job_entry(kio9, data="{data.upstream.s1.data}")})
    )

    assert step.status == STATUS_ERROR
    assert "depend on the step that produces it" in (step.error or "")
    assert not JobHandler.submitted


def test_a_header_reads_its_credential_from_the_environment(
    kio9: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KIO9_TOKEN", "s3cret")
    entry = job_entry(kio9, headers={"Authorization": "Bearer ${KIO9_TOKEN}"})
    review(load_agent_registry({"KIO9": entry}))

    assert JobHandler.headers_seen[0]["Authorization"] == "Bearer s3cret"


# ── the answer ───────────────────────────────────────────────────────────────


def test_accepted_is_waited_through_and_a_pass_reads_as_clean(kio9: str) -> None:
    JobHandler.polls = 3
    step = review(load_agent_registry({"KIO9": job_entry(kio9)}))

    assert step.status == STATUS_OK
    assert step.verdict == "clean"
    assert step.review_required is False


def test_the_agent_s_own_output_reaches_the_step_unchanged(kio9: str) -> None:
    step = review(load_agent_registry({"KIO9": job_entry(kio9)}))

    assert step.output == {"gate": "pass", "verdict": "conformant"}


def test_a_block_is_a_successful_step_with_a_defect_verdict(kio9: str) -> None:
    JobHandler.final = {
        "status": "success",
        "output": {"gate": "block", "verdict": "non_conformant"},
    }
    step = review(load_agent_registry({"KIO9": job_entry(kio9)}))

    assert step.status == STATUS_OK
    assert step.verdict == "defect"


def test_a_warn_asks_for_a_person_and_is_not_clean(kio9: str) -> None:
    JobHandler.final = {"status": "success", "output": {"gate": "warn"}}
    step = review(load_agent_registry({"KIO9": job_entry(kio9)}))

    assert step.review_required is True
    assert step.verdict != "clean"


def test_a_gate_value_kio1_does_not_know_is_not_read_as_a_pass(kio9: str) -> None:
    JobHandler.final = {"status": "success", "output": {"gate": "maybe"}}
    step = review(load_agent_registry({"KIO9": job_entry(kio9)}))

    assert step.review_required is True
    assert step.verdict != "clean"


def test_needing_clarification_stops_for_a_person(kio9: str) -> None:
    JobHandler.final = {
        "status": "needs_clarification",
        "clarification": {"question": "Which release?"},
    }
    step = review(load_agent_registry({"KIO9": job_entry(kio9)}))

    assert step.review_required is True
    assert step.verdict == "inconclusive"
    assert step.output is not None
    assert step.output["clarification"] == {"question": "Which release?"}


def test_a_failed_job_fails_the_step_naming_the_failure_class(kio9: str) -> None:
    JobHandler.final = {
        "status": "failure",
        "failure_class": "hash_mismatch",
        "diagnostics": {"message": "content of a1 does not match its content_hash"},
    }
    step = review(load_agent_registry({"KIO9": job_entry(kio9)}))

    assert step.status == STATUS_ERROR
    assert "hash_mismatch" in (step.error or "")
    assert "content_hash" in (step.error or "")


def test_a_job_that_never_finishes_fails_after_the_entry_s_own_timeout(
    kio9: str,
) -> None:
    JobHandler.polls = 10_000
    entry = job_entry(kio9, poll={"interval_s": 0.05, "timeout_s": 0.3})
    step = review(load_agent_registry({"KIO9": entry}))

    assert step.status == STATUS_ERROR
    assert "did not finish within 0.3s" in (step.error or "")


def test_the_entry_s_timeout_is_used_instead_of_the_global_one(kio9: str) -> None:
    # `review` passes a global timeout of 1 second; three polls of 0.4
    # seconds are allowed because the entry gives itself 5.
    JobHandler.polls = 3
    entry = job_entry(kio9, poll={"interval_s": 0.4, "timeout_s": 5})
    step = review(load_agent_registry({"KIO9": entry}))

    assert step.status == STATUS_OK


def test_an_unreachable_agent_is_a_failed_step_never_a_pass() -> None:
    step = review(load_agent_registry({"KIO9": job_entry("http://127.0.0.1:1")}))

    assert step.status == STATUS_ERROR
    assert step.verdict is None


def test_a_request_the_agent_refuses_is_reported_with_its_reason(kio9: str) -> None:
    # What ERGTECH's KIO9 answered to the execution message: HTTP 200 and its
    # protocol's error object, not a job.
    JobHandler.refuse = {
        "status": "error",
        "error": {
            "code": "malformed_request",
            "message": "/schema_version: Field required",
        },
        "job": None,
    }
    step = review(load_agent_registry({"KIO9": job_entry(kio9)}))

    assert step.status == STATUS_ERROR
    assert "malformed_request" in (step.error or "")
    assert "/schema_version" in (step.error or "")


def test_a_job_entry_goes_over_http_even_when_kio_agents_use_nats(kio9: str) -> None:
    registry = load_agent_registry({"KIO9": job_entry(kio9)})
    transport = build_transport(
        DispatchConfig(enabled=True, registry=registry, transport="nats")
    )
    agent = registry.resolve("KIO9")
    assert agent is not None

    reply = transport.send(message(dict(REVIEW_DATA)), agent, 1)

    assert reply["status"] == "ok"


def test_a_plain_kio_entry_sends_its_configured_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dispatch.agent_transport import HttpAgentTransport

    monkeypatch.delenv("KIO2_TOKEN", raising=False)
    entry = {
        "endpoint": "http://127.0.0.1:1",
        "headers": {"Authorization": "Bearer ${KIO2_TOKEN}"},
    }
    agent = load_agent_registry({"KIO2": entry}).resolve("KIO2")
    assert agent is not None

    with pytest.raises(AgentTransportError, match="KIO2_TOKEN"):
        HttpAgentTransport().send(message({}, agent_id="KIO2"), agent, 1)
