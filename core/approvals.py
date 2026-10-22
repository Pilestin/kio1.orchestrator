"""Runs waiting for a person: to approve a plan, or to answer a question.

Two things make a run stop and wait. A plan that may reach a target that
changes things — a command that writes files, an API that deploys — waits
for a person to approve it. A request the planner cannot act on without
something only the person knows — which city, which file — waits for an
answer. Both are the same mechanism: the run blocks in its own thread until
the console replies or time runs out. Silence counts as no: a plan nobody
looked at is not a plan anybody approved, and a question nobody answered
gives the planner nothing to go on.

Held in memory. A restart forgets what was pending, and the runs waiting on
it, which is the right outcome for a question nobody can answer any more.
"""

import threading
import time
from dataclasses import dataclass, field
from typing import Any

KIND_APPROVAL = "approval"
KIND_QUESTION = "question"


@dataclass
class PendingApproval:
    """One run waiting for a person.

    Attributes:
        session_id: The run.
        round: Which of the run's plans this is about.
        plan: What the console shows. For an approval, the plan's steps and
            the targets that made it need one; for a question, the question.
        asked_at: When the run started waiting.
        timeout: Seconds it waits.
        kind: `approval` or `question`.
    """

    session_id: str
    round: int
    plan: dict[str, Any]
    asked_at: float
    timeout: float
    kind: str = KIND_APPROVAL
    decided: threading.Event = field(default_factory=threading.Event)
    approved: bool = False
    reply: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return what is pending as JSON-ready data."""
        return {
            "session_id": self.session_id,
            "kind": self.kind,
            "round": self.round,
            "plan": self.plan,
            "asked_at": self.asked_at,
            "expires_at": self.asked_at + self.timeout,
        }


class ApprovalDesk:
    """Where runs ask, and where the answers arrive."""

    def __init__(self) -> None:
        """Start with nothing pending."""
        self._pending: dict[str, PendingApproval] = {}
        self._lock = threading.Lock()

    def _wait(self, pending: PendingApproval) -> bool:
        """Hold a run until it is answered or times out.

        Returns:
            Whether an answer arrived in time.
        """
        with self._lock:
            self._pending[pending.session_id] = pending

        try:
            return pending.decided.wait(pending.timeout)
        finally:
            with self._lock:
                if self._pending.get(pending.session_id) is pending:
                    del self._pending[pending.session_id]

    def ask(
        self, session_id: str, round_number: int, plan: dict[str, Any], timeout: float
    ) -> bool:
        """Wait for a plan to be approved or cancelled.

        Called from the run's own thread, which it blocks.

        Args:
            session_id: The run.
            round_number: Which of the run's plans this is.
            plan: What to show the person deciding.
            timeout: Seconds to wait before treating silence as no.

        Returns:
            True when approved; False when cancelled or nobody answered.
        """
        pending = PendingApproval(
            session_id=session_id,
            round=round_number,
            plan=plan,
            asked_at=time.time(),
            timeout=timeout,
        )

        return self._wait(pending) and pending.approved

    def ask_question(
        self, session_id: str, round_number: int, question: str, timeout: float
    ) -> str | None:
        """Wait for the person to answer a question.

        Args:
            session_id: The run.
            round_number: The round being planned.
            question: What the planner needs to know.
            timeout: Seconds to wait.

        Returns:
            The answer, or None when nobody answered in time.
        """
        pending = PendingApproval(
            session_id=session_id,
            round=round_number,
            plan={"question": question},
            asked_at=time.time(),
            timeout=timeout,
            kind=KIND_QUESTION,
        )

        return pending.reply if self._wait(pending) else None

    def pending(self, session_id: str) -> PendingApproval | None:
        """Return what a run is waiting on, or None when it is not waiting."""
        with self._lock:
            return self._pending.get(session_id)

    def _take(self, session_id: str, kind: str) -> PendingApproval | None:
        """Remove and return what a run waits on, when it is of this kind."""
        with self._lock:
            pending = self._pending.get(session_id)

            if pending is None or pending.kind != kind:
                return None

            return self._pending.pop(session_id)

    def decide(self, session_id: str, approved: bool) -> bool:
        """Approve or cancel a waiting plan.

        Args:
            session_id: The run.
            approved: Whether its plan may run.

        Returns:
            True when a plan was waiting and has been answered; False when
            there was nothing to answer — already decided, timed out, never
            asked, or the run is waiting on a question instead.
        """
        pending = self._take(session_id, KIND_APPROVAL)

        if pending is None:
            return False

        pending.approved = approved
        pending.decided.set()

        return True

    def answer(self, session_id: str, reply: str) -> bool:
        """Answer a waiting question.

        Args:
            session_id: The run.
            reply: The person's answer.

        Returns:
            True when a question was waiting and has been answered.
        """
        pending = self._take(session_id, KIND_QUESTION)

        if pending is None:
            return False

        pending.reply = reply
        pending.decided.set()

        return True
