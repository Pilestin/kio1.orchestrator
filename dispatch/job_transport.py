"""The KIO1 job interface: post a step, then poll until it has an answer.

Some KIOs cannot answer inside one HTTP request. KIO9 reviews a report for
15–40 seconds and works through one review at a time, so it speaks the job
interface of BITNET §3.5 instead of the execution-message contract:

1. `POST {endpoint}{path}` carries a *job request* — `schema_version` "1.0",
   the step's ids, its capability, task and `data` — and is answered at once
   with a job envelope: `status: accepted` and a `job_id`.
2. `GET {endpoint}/jobs/{job_id}` is polled until the status is final:
   `success` (the result is in `output`), `needs_clarification`, or
   `failure` (`failure_class` and `diagnostics` say why).

This transport does both and hands the executor the same reply shape every
other transport does, so a plan cannot tell a job from a direct answer.

Two things here exist so that a compliance gate can never read as passed
when it did not pass:

* `accepted` is progress, never a result. Only a final state ends the step,
  and the entry's own timeout, not a guess, decides when to stop waiting.
  Running out of time, an unreachable agent, and a failed job are all failed
  steps.
* The job ids are derived from the request's content. KIO9 answers a repeated
  `workflow_id` + `step_id` with the job it already has, and a planner reuses
  workflow ids freely (`wf-weather01` appears in two unrelated runs). Sent as
  they are, a second report would be answered with the first one's review.
"""

import hashlib
import json
import logging
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from opentelemetry.propagate import inject

from dispatch.agent_registry import AgentEndpoint, KioJobSpec
from dispatch.agent_transport import AgentTransportError
from dispatch.tool_transport import expand_environment, protocol_reply, render
from dispatch.workflow_planner import ExecutionMessage

logger = logging.getLogger(__name__)

JOB_SCHEMA_VERSION = "1.0"

JOB_ACCEPTED = "accepted"
JOB_SUCCESS = "success"
JOB_NEEDS_CLARIFICATION = "needs_clarification"
JOB_FAILURE = "failure"
_FINAL = frozenset({JOB_SUCCESS, JOB_NEEDS_CLARIFICATION, JOB_FAILURE})

# What a reviewer's gate means for the step, in the executor's terms. `warn`
# is neither: it passes only once a person acknowledges it.
_GATE_VERDICTS = {"pass": "clean", "block": "defect"}
_GATE_NEEDS_A_PERSON = "warn"

# The job interface allows ids of this alphabet, at most 64 characters.
_ID_FORBIDDEN = re.compile(r"[^A-Za-z0-9._-]+")
_ID_MAX = 64
_DIGEST_CHARS = 12

# One request is never allowed to take longer than this, even when the job's
# deadline is further away: a hung connection should cost one poll, not the
# whole budget.
_REQUEST_TIMEOUT_CAP = 30.0


def _id_part(value: str, limit: int) -> str:
    """Return `value` in the job interface's id alphabet, at most `limit` long."""
    cleaned = _ID_FORBIDDEN.sub("-", value).strip("-") or "step"
    return cleaned[:limit]


def job_request(message: ExecutionMessage, spec: KioJobSpec) -> dict[str, Any]:
    """Build the job request for one step.

    Args:
        message: The step being dispatched.
        spec: The entry's job settings, whose `data` template, if any, says
            what to send as `data`.

    Returns:
        The job request body.

    Raises:
        AgentTransportError: If the `data` template does not resolve to an
            object — typically because the step does not depend on the step
            that produced the payload, so there was nothing to fill it with.
    """
    data = message.data if spec.data is None else render(spec.data, message)

    if not isinstance(data, dict):
        raise AgentTransportError(
            f"{message.agent_id} needs its job data as an object, and the "
            f"configured data {json.dumps(spec.data, ensure_ascii=False)} did not "
            "resolve to one. Does the step depend on the step that produces it?"
        )

    # The same step with the same content is the same job, so a retry or a
    # failover finds the job already there; anything else is a new one.
    canonical = json.dumps(
        {"capability": message.capability, "task": message.task, "data": data},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    return {
        "schema_version": JOB_SCHEMA_VERSION,
        "workflow_id": _id_part(message.workflow_id, _ID_MAX),
        "step_id": (
            f"{_id_part(message.step_id, _ID_MAX - _DIGEST_CHARS - 1)}"
            f"-{digest[:_DIGEST_CHARS]}"
        ),
        "capability": message.capability,
        "task": message.task,
        "data": data,
        "idempotency_key": f"kio1-{digest[:32]}",
    }


def step_reply(
    message: ExecutionMessage, agent: AgentEndpoint, job: dict[str, Any]
) -> dict[str, Any]:
    """Turn a job in a final state into the reply the executor reads.

    `verdict` and `hitl_required` are set beside `output` rather than inside
    it, so the agent's own output reaches the next step unchanged — KIO9's
    `output.verdict` is a conformance verdict with values of its own.

    Args:
        message: The step this answers.
        agent: The entry it was sent to, for messages.
        job: The final job envelope.

    Returns:
        A reply in the KIO1-KIO10 shape.
    """
    status = job.get("status")
    job_id = job.get("job_id", "?")

    if status == JOB_FAILURE:
        diagnostics = job.get("diagnostics")
        detail = diagnostics.get("message") if isinstance(diagnostics, dict) else None
        reason = job.get("failure_class") or "unspecified"
        return protocol_reply(
            message,
            status="error",
            error=(
                f"{agent.agent_id} job {job_id} failed: {reason}"
                + (f" — {detail}" if detail else "")
            ),
        )

    if status == JOB_NEEDS_CLARIFICATION:
        reply = protocol_reply(
            message,
            status="ok",
            output={
                "job_id": job_id,
                "status": status,
                "clarification": job.get("clarification"),
            },
        )
        reply.update(verdict="inconclusive", hitl_required=True)
        return reply

    output = job.get("output")
    if not isinstance(output, dict):
        output = {"result": output}

    reply = protocol_reply(message, status="ok", output=output)

    if "gate" in output:
        gate = output.get("gate")

        if gate in _GATE_VERDICTS:
            reply["verdict"] = _GATE_VERDICTS[gate]
        else:
            # `warn` waits for a person by definition; a gate value KIO1 does
            # not know is treated the same way rather than as a pass.
            if gate != _GATE_NEEDS_A_PERSON:
                logger.warning(
                    "Unknown gate from %s: %r, treated as needing review",
                    agent.agent_id,
                    gate,
                )
            reply["hitl_required"] = True

    return reply


class KioJobTransport:
    """Runs steps on KIO agents that answer through the KIO1 job interface."""

    def __init__(
        self,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create the transport.

        Args:
            sleep: Waits between polls. Replaced in tests.
            clock: Reads the time a job's deadline is measured against.
                Replaced in tests.
        """
        self._sleep = sleep
        self._clock = clock

    def close(self) -> None:
        """Do nothing: each request opens and closes its own connection."""

    def send(
        self,
        message: ExecutionMessage,
        agent: AgentEndpoint,
        timeout: float,
    ) -> dict[str, Any]:
        """Submit the step as a job and wait for its final state.

        Args:
            message: The execution message to deliver.
            agent: The resolved entry, carrying a `KioJobSpec`.
            timeout: The global step timeout. Not used: a job entry carries
                its own, because the global one is sized for direct answers.

        Returns:
            The reply in the KIO1-KIO10 shape.

        Raises:
            AgentTransportError: If the entry is not a job entry, the agent is
                unreachable, or it answered with something that is not a job.
        """
        spec = agent.jobs

        if spec is None:
            raise AgentTransportError(f"{agent.agent_id} is not a job entry.")

        request = job_request(message, spec)
        headers = {
            name: expand_environment(
                str(render(value, message)),
                agent_id=agent.agent_id,
                field=f"header {name}",
            )
            for name, value in agent.headers.items()
        }
        deadline = self._clock() + spec.timeout

        job = self._call(agent, "POST", agent.url, headers, deadline, request)
        job_id = job.get("job_id")
        logger.info(
            "Job submitted: agent_id=%s step_id=%s job_id=%s status=%s",
            agent.agent_id,
            request["step_id"],
            job_id,
            job.get("status"),
        )

        while job.get("status") not in _FINAL:
            if not isinstance(job_id, str) or not job_id:
                raise AgentTransportError(
                    f"{agent.agent_id} answered status {job.get('status')!r} "
                    "without a job_id, so there is nothing to poll."
                )

            if self._clock() + spec.interval > deadline:
                return protocol_reply(
                    message,
                    status="error",
                    error=(
                        f"{agent.agent_id} job {job_id} did not finish within "
                        f"{spec.timeout:g}s (last status: {job.get('status')!r})."
                    ),
                )

            self._sleep(spec.interval)
            poll_url = agent.endpoint + spec.poll_path.replace(
                "{job_id}", quote(job_id, safe="")
            )
            job = self._call(agent, "GET", poll_url, headers, deadline)

        logger.info(
            "Job finished: agent_id=%s job_id=%s status=%s",
            agent.agent_id,
            job_id,
            job.get("status"),
        )
        return step_reply(message, agent, job)

    def _call(
        self,
        agent: AgentEndpoint,
        method: str,
        url: str,
        headers: dict[str, str],
        deadline: float,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make one request and return the job envelope it answered with.

        Args:
            agent: The entry, for messages.
            method: `POST` to submit, `GET` to poll.
            url: The full URL.
            headers: Configured headers, already expanded.
            deadline: When the job's time runs out, on this transport's clock.
            body: The job request, for a submission.

        Returns:
            The decoded job envelope.

        Raises:
            AgentTransportError: If the request failed, or the answer is not a
                job envelope. A KIO that rejects the request outright answers
                with its protocol's error object instead of a job, and that
                is reported with the code and message it gave.
        """
        sent_headers = {"Accept": "application/json", **headers}
        data = None

        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            sent_headers.setdefault("Content-Type", "application/json")

        inject(sent_headers)
        remaining = max(1.0, min(_REQUEST_TIMEOUT_CAP, deadline - self._clock()))
        request = urllib.request.Request(
            url, data=data, headers=sent_headers, method=method
        )

        try:
            with urllib.request.urlopen(request, timeout=remaining) as response:
                text = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as error:
            detail = " ".join(error.read().decode("utf-8", errors="replace").split())
            raise AgentTransportError(
                f"{agent.agent_id} returned HTTP {error.code} for {method} {url}: "
                f"{detail[:500] or '<empty body>'}"
            ) from error
        except urllib.error.URLError as error:
            raise AgentTransportError(
                f"{agent.agent_id} is unreachable at {url}: {error.reason}"
            ) from error
        except TimeoutError as error:
            raise AgentTransportError(
                f"{agent.agent_id} did not answer {method} {url} within {remaining:g}s"
            ) from error

        try:
            decoded: Any = json.loads(text)
        except json.JSONDecodeError as error:
            raise AgentTransportError(
                f"{agent.agent_id} returned a non-JSON answer to {method} {url}: {error}"
            ) from error

        if not isinstance(decoded, dict):
            raise AgentTransportError(
                f"{agent.agent_id} returned {type(decoded).__name__} to {method} "
                f"{url}, expected a job envelope."
            )

        if decoded.get("status") == "error":
            # KIO9 v0.4's reply envelope, sent when the request is not one it
            # can turn into a job: `/schema_version: Field required`, and so on.
            problem = decoded.get("error")
            if isinstance(problem, dict):
                text = f"{problem.get('code', 'error')}: {problem.get('message', '')}".strip()
            else:
                text = str(problem or "unspecified error")
            raise AgentTransportError(f"{agent.agent_id} refused the job: {text}")

        if "status" not in decoded:
            raise AgentTransportError(
                f"{agent.agent_id} answered {method} {url} without a job status."
            )

        return decoded
