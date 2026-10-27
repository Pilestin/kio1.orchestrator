"""Stopping a run that is under way.

A run is a thread working through planning, steps, and a review, and most of
that time is spent waiting on something else: a model writing, an agent
answering, a person deciding. None of those can be interrupted from outside
safely, so stopping is a request the run honours at its next safe point —
between rounds, between steps, after each token a streaming model writes,
and the moment a wait for a person ends. What is already in flight finishes;
nothing after it starts.

The request travels in a context variable rather than through every call: the
executor copies the context into its worker threads already, for tracing, so
a step about to be dispatched in parallel can ask too.
"""

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

# What a stopped run says, in the console and to a later request in its
# conversation.
STOPPED_REASON = "Stopped."


class RunStopped(Exception):
    """The run was asked to stop, and has reached a point where it can."""


class StopBoard:
    """Which runs are under way, and which of them were asked to stop."""

    def __init__(self) -> None:
        """Start with nothing running."""
        self._running: set[str] = set()
        self._stopping: set[str] = set()
        self._lock = threading.Lock()

    def begin(self, session_id: str) -> None:
        """Note that a run started."""
        with self._lock:
            self._running.add(session_id)

    def end(self, session_id: str) -> None:
        """Note that a run is over, stopped or not."""
        with self._lock:
            self._running.discard(session_id)
            self._stopping.discard(session_id)

    def stop(self, session_id: str) -> bool:
        """Ask a run to stop.

        Returns:
            True when it is running and will stop; False when it is not
            running — finished, never started, or started by a process that
            is gone.
        """
        with self._lock:
            if session_id not in self._running:
                return False

            self._stopping.add(session_id)
            return True

    def stopping(self, session_id: str) -> bool:
        """Return whether a run was asked to stop."""
        with self._lock:
            return session_id in self._stopping


_current: ContextVar[tuple[StopBoard, str] | None] = ContextVar(
    "kio1_stoppable_run", default=None
)


@contextmanager
def stoppable(board: StopBoard | None, session_id: str) -> Iterator[None]:
    """Make the run in this context one that can be stopped.

    Args:
        board: Where stop requests arrive. None makes the run unstoppable,
            and every check below a no-op — the terminal application, tests.
        session_id: The run.
    """
    if board is None:
        yield
        return

    board.begin(session_id)
    token = _current.set((board, session_id))

    try:
        yield
    finally:
        _current.reset(token)
        board.end(session_id)


def stop_requested() -> bool:
    """Return whether the run in this context was asked to stop."""
    current = _current.get()
    return current is not None and current[0].stopping(current[1])


def check_stop() -> None:
    """Raise `RunStopped` when the run in this context was asked to stop."""
    if stop_requested():
        raise RunStopped()
