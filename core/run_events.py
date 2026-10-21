"""What a run is doing, as it does it.

A run takes minutes on a laptop: the model reads the catalog, writes a plan
token by token, then each step goes out and comes back. The Session Manager
records the outcome, but nothing in between — and a page that says
`running` for ninety seconds looks exactly like one that has hung.

This module carries the in-between. Code anywhere in a run calls `emit()`;
the event lands in a bounded per-run buffer; the console reads it as a
stream. Nothing here is persisted: the outcome already is, and a live view
of a run that finished an hour ago is what the run's record is for.

The current run is held in a context variable rather than passed down, so
the executor, the transports, and the provider can report without every
signature between them growing a parameter. The executor already runs
parallel steps under `contextvars.copy_context()`, so it follows them there.
"""

import logging
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

# Events a run's stream ends with. A reader that has seen one can stop.
FINISHED = "finished"

# Loggers whose records are relayed into a run's stream: KIO1's own, not
# every library that happens to log while a run is in progress.
_RELAYED_LOGGERS = ("app", "core", "dispatch", "providers", "services", "tools")


@dataclass(frozen=True)
class RunEvent:
    """One thing that happened during a run.

    Attributes:
        id: Position in the run's stream, from 1. A reader resumes after the
            last id it saw.
        kind: What happened, such as `plan` or `step_finished`.
        time: Wall-clock seconds.
        data: The details, JSON-ready.
    """

    id: int
    kind: str
    time: float
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return the event as JSON-ready data."""
        return {"id": self.id, "kind": self.kind, "time": self.time, **self.data}


class _Stream:
    """The events of one run."""

    def __init__(self, capacity: int) -> None:
        self.events: deque[RunEvent] = deque(maxlen=capacity)
        self.next_id = 1
        self.finished = False


class RunEventBus:
    """Holds recent runs' events and wakes whoever is waiting for more.

    Bounded twice over: each run keeps its latest `events_per_run` events,
    and only the latest `runs` runs are kept at all. A model streaming a long
    answer, or a service left up for a month, cannot grow it without limit.
    """

    def __init__(self, *, events_per_run: int = 4000, runs: int = 50) -> None:
        """Create an empty bus.

        Args:
            events_per_run: Events kept per run; older ones are dropped.
            runs: Runs kept; the oldest is dropped when another starts.
        """
        self._events_per_run = events_per_run
        self._runs = runs
        self._streams: OrderedDict[str, _Stream] = OrderedDict()
        self._changed = threading.Condition()

    def publish(self, run_id: str, kind: str, **data: Any) -> RunEvent:
        """Add an event to a run's stream.

        Args:
            run_id: The run, normally its session id.
            kind: What happened.
            **data: JSON-ready details.

        Returns:
            The stored event.
        """
        with self._changed:
            stream = self._streams.get(run_id)

            if stream is None:
                stream = _Stream(self._events_per_run)
                self._streams[run_id] = stream

                while len(self._streams) > self._runs:
                    self._streams.popitem(last=False)

            event = RunEvent(id=stream.next_id, kind=kind, time=time.time(), data=data)
            stream.next_id += 1
            stream.events.append(event)

            if kind == FINISHED:
                stream.finished = True

            self._changed.notify_all()

            return event

    def read(self, run_id: str, after: int = 0) -> list[RunEvent]:
        """Return a run's events with an id greater than `after`."""
        with self._changed:
            return self._unread(run_id, after)

    def wait(self, run_id: str, after: int, timeout: float) -> list[RunEvent]:
        """Return new events, waiting up to `timeout` seconds for some.

        Args:
            run_id: The run.
            after: The last id the reader has.
            timeout: Seconds to wait when there is nothing new.

        Returns:
            The new events, empty when none arrived in time.
        """
        deadline = time.monotonic() + timeout

        with self._changed:
            while True:
                unread = self._unread(run_id, after)
                remaining = deadline - time.monotonic()

                if unread or remaining <= 0 or self._is_finished(run_id):
                    return unread

                self._changed.wait(remaining)

    def is_finished(self, run_id: str) -> bool:
        """Report whether a run's stream has ended."""
        with self._changed:
            return self._is_finished(run_id)

    def _is_finished(self, run_id: str) -> bool:
        stream = self._streams.get(run_id)
        return stream is not None and stream.finished

    def _unread(self, run_id: str, after: int) -> list[RunEvent]:
        stream = self._streams.get(run_id)

        if stream is None:
            return []

        return [event for event in stream.events if event.id > after]


_current: ContextVar[tuple[RunEventBus, str] | None] = ContextVar(
    "kio1_current_run", default=None
)


@contextmanager
def observing(bus: RunEventBus | None, run_id: str) -> Iterator[None]:
    """Make `emit()` report to a run for the duration of a block.

    Args:
        bus: Where events go. None makes the block a no-op, so callers that
            have no bus need no branch of their own.
        run_id: The run being observed.
    """
    if bus is None:
        yield
        return

    token = _current.set((bus, run_id))

    try:
        yield
    finally:
        _current.reset(token)


def emit(kind: str, **data: Any) -> None:
    """Report an event to the run in progress, if there is one.

    Safe to call from anywhere: outside a run it does nothing, which is what
    the terminal application and the tests want.
    """
    current = _current.get()

    if current is not None:
        bus, run_id = current
        bus.publish(run_id, kind, **data)


def observed() -> bool:
    """Report whether a run is being observed in this context."""
    return _current.get() is not None


class TokenRelay:
    """Turns a model's token stream into a manageable number of events.

    A model writes a few tokens a second on a CPU and hundreds a second on a
    GPU. One event per token would bury every other event, so text is
    gathered and sent at most every `interval` seconds — fast enough to read
    as live.
    """

    def __init__(self, interval: float = 0.25) -> None:
        """Start an empty relay."""
        self._interval = interval
        self._pending: dict[str, list[str]] = {}
        self._last = time.monotonic()

    def __call__(self, text: str, channel: str = "content") -> None:
        """Accept a fragment of model output.

        Args:
            text: The fragment.
            channel: `content` for the answer, `thinking` for a reasoning
                model's thinking.
        """
        self._pending.setdefault(channel, []).append(text)

        if time.monotonic() - self._last >= self._interval:
            self.flush()

    def flush(self) -> None:
        """Send whatever has been gathered."""
        for channel, parts in self._pending.items():
            if parts:
                emit("model_output", channel=channel, text="".join(parts))

        self._pending = {}
        self._last = time.monotonic()


class RunLogHandler(logging.Handler):
    """Relays KIO1's log records into the stream of the run that wrote them.

    The logs already say what the router chose, which target was tried next,
    and why a step was skipped. Showing them beside the run is cheaper than
    inventing an event for each.
    """

    def __init__(self, level: int = logging.INFO) -> None:
        """Create a handler passing records at `level` and above."""
        super().__init__(level)

    def emit(self, record: logging.LogRecord) -> None:
        """Forward one record, when it belongs to KIO1 and to a run."""
        if not observed() or not record.name.startswith(_RELAYED_LOGGERS):
            return

        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a bad format string must not break a run
            message = str(record.msg)

        emit("log", level=record.levelname, logger=record.name, message=message)


def install_log_relay() -> Callable[[], None]:
    """Attach a `RunLogHandler` to the root logger.

    KIO1's modules log at INFO, which the root logger drops by default, so
    the root is lowered to INFO when it is above it. That must not make the
    terminal noisier, and adding any handler to the root silences Python's
    fallback output, which is what printed warnings until now. So when the
    root had no handlers, a stderr handler at WARNING takes the fallback's
    place, and the terminal shows exactly what it showed before.

    Returns:
        A function removing what was added and restoring the level.
    """
    root = logging.getLogger()
    previous_level = root.level
    added: list[logging.Handler] = [RunLogHandler()]

    if not root.handlers:
        terminal = logging.StreamHandler()
        terminal.setLevel(logging.WARNING)
        terminal.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        added.append(terminal)

    if root.level > logging.INFO:
        root.setLevel(logging.INFO)

    for handler in added:
        root.addHandler(handler)

    def remove() -> None:
        for handler in added:
            root.removeHandler(handler)

        root.setLevel(previous_level)

    return remove
