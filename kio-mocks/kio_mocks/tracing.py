"""KIO2's engine: record an execution, then read a fault out of it.

D2.6 asks KIO2 for a trace recorder (FR-KIO2-07), replay of recorded traces
(FR-KIO2-02/03), and fault localisation from replayed traces and slice
context (FR-KIO2-05) — not for fixes, which UC-UC1-03 gives to KIO7. Here:

- `record` runs a program or a test under `_tracer.py`, in its own process,
  and keeps the trace under a reference (`kio-mocks://trace/<id>`) a later
  step can be given through `data.upstream`.
- `localise` finds where the failing exception was raised — not where it
  surfaced — and takes a **dynamic slice** back from that line: the names it
  uses, the lines that last set them, and theirs in turn, across the calls
  that brought the values in.
- `replay` steps through a recording; `align` lines two recordings up and
  finds where they part.
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kio_mocks.errors import KioError
from kio_mocks.workspace import Workspace, python

TRACER = Path(__file__).with_name("_tracer.py")
TRACE_PREFIX = "kio-mocks://trace/"
TRACE_DIR = Path(
    os.environ.get("KIO_MOCKS_TRACES")
    or Path(tempfile.gettempdir()) / "kio-mocks-traces"
)

# How far back a slice follows definitions: far enough for a value passed
# through a few helpers, short enough to stay something a person reads.
SLICE_DEPTH = 6


@dataclass(frozen=True)
class Trace:
    ref: str
    data: dict[str, Any]

    @property
    def events(self) -> list[dict[str, Any]]:
        return self.data["events"]

    @property
    def outcome(self) -> dict[str, Any]:
        return self.data["outcome"]

    @property
    def failed(self) -> bool:
        return "raised_at" in self.outcome


def record(
    workspace: Workspace, mode: str, target: str, *, timeout: float = 40.0
) -> Trace:
    """Run `target` under the tracer and keep what it recorded.

    Args:
        workspace: Where the program lives and runs.
        mode: `pytest` for a test node id, `script` for a Python file.
        target: `tests/test_x.py::test_y`, or `main.py`.
        timeout: Seconds the run may take.
    """
    root = workspace.require()
    TRACE_DIR.mkdir(parents=True, exist_ok=True)
    trace_id = hashlib.sha256(
        f"{root}|{mode}|{target}|{time.time_ns()}".encode()
    ).hexdigest()[:16]
    out = TRACE_DIR / f"{trace_id}.json"

    completed = workspace.run(
        [python(), str(TRACER), str(root), mode, target, str(out)], timeout=timeout
    )

    if not out.is_file():
        raise KioError(
            f"Recording {target} produced no trace: "
            + (completed.stderr.strip().splitlines() or ["no output"])[-1]
        )

    return Trace(
        ref=TRACE_PREFIX + trace_id, data=json.loads(out.read_text(encoding="utf-8"))
    )


def load(ref: str) -> Trace:
    """A recording made earlier, by its reference."""
    if not isinstance(ref, str) or not ref.startswith(TRACE_PREFIX):
        raise KioError(f"{ref!r} is not a trace reference from this KIO2.")

    trace_id = ref[len(TRACE_PREFIX) :]
    path = TRACE_DIR / f"{trace_id}.json"

    if not trace_id.isalnum() or not path.is_file():
        raise KioError(f"No recording {ref}; it may be from before a restart.")

    return Trace(ref=ref, data=json.loads(path.read_text(encoding="utf-8")))


# ── reading a fault out of a trace ───────────────────────────────────────────


def _source_line(workspace: Workspace, file: str, line: int) -> str:
    try:
        lines = workspace.read(file).splitlines()
    except KioError:
        return ""
    return lines[line - 1].strip() if 0 < line <= len(lines) else ""


def _names_used(code: str) -> list[str]:
    """The variable names a line reads, in the order they appear."""
    try:
        tree = ast.parse(code.rstrip(":") if code.rstrip().endswith(":") else code)
    except SyntaxError:
        try:
            tree = ast.parse(code.rstrip().rstrip(":") + ": pass")
        except SyntaxError:
            return []

    names: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id not in names
        ):
            names.append(node.id)
    return names


def _values_at(trace: Trace, frame: int, index: int) -> dict[str, str]:
    """What each variable of a frame held when event `index` was reached."""
    values: dict[str, str] = {}
    for event in trace.events[: index + 1]:
        if event["frame"] == frame:
            values.update(event.get("defined") or {})
    return values


def _call_of(trace: Trace, frame: int) -> dict[str, Any] | None:
    return next(
        (
            event
            for event in trace.events
            if event["frame"] == frame and event["kind"] == "call"
        ),
        None,
    )


def stack_at(trace: Trace, index: int) -> list[dict[str, Any]]:
    """The calls that led to event `index`, outermost first."""
    frames: list[dict[str, Any]] = []
    frame = trace.events[index]["frame"]

    while frame:
        call = _call_of(trace, frame)
        if call is None:
            break
        # Where this frame was when the event happened.
        here = max(
            (
                event
                for event in trace.events[: index + 1]
                if event["frame"] == frame
                and event["kind"] in ("call", "line", "exception")
            ),
            key=lambda event: event["i"],
        )
        frames.append(
            {
                "file": here["file"],
                "line": here["line"],
                "function": here["function"],
                "frame": frame,
            }
        )
        frame = call.get("caller")

    return list(reversed(frames))


def localise(workspace: Workspace, trace: Trace) -> dict[str, Any]:
    """Where the failure was raised, and the slice of execution that led to it."""
    if not trace.failed:
        return {"failed": False}

    raised = trace.events[trace.outcome["raised_at"]]
    code = _source_line(workspace, raised["file"], raised["line"])
    values = _values_at(trace, raised["frame"], raised["i"])

    slice_lines: dict[tuple[str, int], dict[str, Any]] = {}
    wanted = [(name, raised["frame"], raised["i"]) for name in _names_used(code)]
    seen: set[tuple[str, int]] = set()
    depth = 0

    while wanted and depth < SLICE_DEPTH:
        following = []
        for name, frame, before in wanted:
            if (name, frame) in seen:
                continue
            seen.add((name, frame))
            definition = next(
                (
                    event
                    for event in reversed(trace.events[:before])
                    if event["frame"] == frame and name in (event.get("defined") or {})
                ),
                None,
            )
            if definition is None:
                continue

            key = (definition["file"], definition["line"])
            line_code = _source_line(workspace, *key)
            entry = slice_lines.setdefault(
                key,
                {
                    "file": key[0],
                    "line": key[1],
                    "code": line_code,
                    "function": definition["function"],
                    "defines": {},
                },
            )
            entry["defines"][name] = definition["defined"][name]

            if definition["kind"] == "call":
                # A parameter: its value came from the caller's arguments.
                caller = definition.get("caller")
                call_site = next(
                    (
                        event
                        for event in reversed(trace.events[: definition["i"]])
                        if event["frame"] == caller
                    ),
                    None,
                )
                if call_site is not None:
                    site = (call_site["file"], call_site["line"])
                    slice_lines.setdefault(
                        site,
                        {
                            "file": site[0],
                            "line": site[1],
                            "code": _source_line(workspace, *site),
                            "function": call_site["function"],
                            "defines": {},
                        },
                    )
                    following += [
                        (used, caller, call_site["i"] + 1)
                        for used in _names_used(slice_lines[site]["code"])
                    ]
            else:
                following += [
                    (used, frame, definition["i"]) for used in _names_used(line_code)
                ]
        wanted = following
        depth += 1

    used = _names_used(code)

    return {
        "failed": True,
        "exception": trace.outcome.get("exception", {}),
        "fault": {
            "file": raised["file"],
            "line": raised["line"],
            "function": raised["function"],
            "code": code,
            "values": {name: values[name] for name in used if name in values},
        },
        "stack": stack_at(trace, raised["i"]),
        "slice": sorted(
            slice_lines.values(), key=lambda entry: (entry["file"], entry["line"])
        ),
    }


def replay(
    workspace: Workspace, trace: Trace, start: int | None = None, count: int = 25
) -> list[dict[str, Any]]:
    """Steps of a recording, from `start` or leading up to the failure."""
    steps = [
        event
        for event in trace.events
        if event["kind"] in ("call", "line", "exception", "return")
    ]

    if start is None:
        end = trace.outcome.get("raised_at", steps[-1]["i"] if steps else 0)
        start = max(0, end - count + 1)

    window = [event for event in steps if event["i"] >= start][:count]

    return [
        {
            "step": event["i"],
            "event": event["kind"],
            "at": f"{event['file']}:{event['line']}",
            "function": event["function"],
            "code": (
                _source_line(workspace, event["file"], event["line"])
                if event["kind"] != "call"
                else ""
            ),
            "changed": event.get("defined") or {},
            **(
                {"exception": f"{event['type']}: {event['message']}"}
                if event["kind"] == "exception"
                else {}
            ),
        }
        for event in window
    ]


def align(first: Trace, second: Trace) -> dict[str, Any]:
    """Line two recordings up; where do they stop doing the same thing?"""

    def path(trace: Trace) -> list[tuple[str, int, str]]:
        return [
            (event["file"], event["line"], event["function"])
            for event in trace.events
            if event["kind"] == "line"
        ]

    a, b = path(first), path(second)
    matcher = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    blocks = matcher.get_matching_blocks()
    common = blocks[0].size if blocks and blocks[0].a == 0 and blocks[0].b == 0 else 0

    divergence = None
    if common < min(len(a), len(b)) or len(a) != len(b):
        divergence = {
            "after_common_steps": common,
            "first": "{}:{} in {}".format(*a[common]) if common < len(a) else "(ended)",
            "second": (
                "{}:{} in {}".format(*b[common]) if common < len(b) else "(ended)"
            ),
        }

    return {
        "similarity": round(matcher.ratio(), 3),
        "steps": [len(a), len(b)],
        "common_prefix": common,
        "divergence": divergence,
    }
