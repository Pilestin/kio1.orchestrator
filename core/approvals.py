"""Plans waiting for a person to say yes before they run.

A plan the model wrote can reach a target that changes things — a command
that writes files, an API that deploys. For those, a person sees the plan
first and approves or cancels it. The run waits in its own thread; the
console answers through the API; whichever comes first, the answer or the
timeout, ends the wait. A timeout counts as no: a plan nobody looked at is
not a plan anybody approved.

Held in memory. A restart forgets pending approvals, and the runs waiting on
them, which is the right outcome for a question nobody can answer any more.
"""

import threading
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PendingApproval:
    """One plan waiting for an answer.

    Attributes:
        session_id: The run.
        round: Which of the run's plans this is.
        plan: What the console shows: the plan's steps and the targets that
            made it need approval.
        asked_at: When the run started waiting.
        timeout: Seconds it waits.
    """

    session_id: str
    round: int
    plan: dict[str, Any]
    asked_at: float
    timeout: float
    decided: threading.Event = field(default_factory=threading.Event)
    approved: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return the pending approval as JSON-ready data."""
        return {
            "session_id": self.session_id,
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

        with self._lock:
            self._pending[session_id] = pending

        try:
            answered = pending.decided.wait(timeout)
            return answered and pending.approved
        finally:
            with self._lock:
                if self._pending.get(session_id) is pending:
                    del self._pending[session_id]

    def pending(self, session_id: str) -> PendingApproval | None:
        """Return what a run is waiting on, or None when it is not waiting."""
        with self._lock:
            return self._pending.get(session_id)

    def decide(self, session_id: str, approved: bool) -> bool:
        """Answer a waiting run.

        Args:
            session_id: The run.
            approved: Whether its plan may run.

        Returns:
            True when a run was waiting and has been answered; False when
            there was nothing to answer — already decided, timed out, or
            never asked.
        """
        with self._lock:
            pending = self._pending.pop(session_id, None)

        if pending is None:
            return False

        pending.approved = approved
        pending.decided.set()

        return True
