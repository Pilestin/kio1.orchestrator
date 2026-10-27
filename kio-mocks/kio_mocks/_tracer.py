"""Run a program or one test under `sys.settrace`, and write what happened.

Started as a script by `tracing.py`, in the workspace and in a process of its
own: the code being traced is the project's, and whatever it does must not
happen inside the service. Standard library only, because it runs with
whatever interpreter the project has.

    python _tracer.py <root> <script|pytest> <target> <out.json>

Recorded, for the project's own files only (never a virtual environment's):
every call, line, return and exception, with the variables that changed —
attributed to the line that changed them, which is what a dynamic slice
needs — and the identity of each exception, so the place it was raised can
be told apart from the frames it passed through on its way out.
"""

import json
import os
import runpy
import sys
import threading
import types

SKIPPED = {
    ".git",
    ".venv",
    "venv",
    "env",
    "site-packages",
    "node_modules",
    "__pycache__",
    "build",
    "dist",
}
MAX_EVENTS = 60_000
MAX_REPR = 160


def main() -> None:
    root, mode, target, out = sys.argv[1:5]
    root = os.path.realpath(root)
    sys.path.insert(0, root)
    os.chdir(root)

    events: list[dict] = []
    frame_ids: dict[int, int] = {}
    last_line: dict[int, int] = {}
    last_values: dict[int, dict] = {}
    ours_cache: dict[str, str | None] = {}
    state: dict = {"truncated": False, "failing": None}

    def relative(filename: str) -> str | None:
        if filename in ours_cache:
            return ours_cache[filename]
        name = None
        # "<frozen importlib._bootstrap>", "<string>": not files, and realpath
        # would resolve them against the workspace.
        path = os.path.realpath(filename) if os.path.isabs(filename) else ""
        if path.startswith(root + os.sep):
            rest = path[len(root) + 1 :]
            if not any(part in SKIPPED for part in rest.split(os.sep)):
                name = rest.replace(os.sep, "/")
        ours_cache[filename] = name
        return name

    def short(value: object) -> str:
        try:
            text = repr(value)
        except Exception:  # noqa: BLE001 - a broken __repr__ is the project's
            text = "<unrepresentable>"
        return text if len(text) <= MAX_REPR else text[: MAX_REPR - 3] + "..."

    def snapshot(frame) -> dict:
        # Values, not the functions, classes and modules a frame also holds, nor
        # pytest's assertion temporaries (@py_assert1).
        return {
            name: short(value)
            for name, value in frame.f_locals.items()
            if not name.startswith(("__", "@"))
            and not callable(value)
            and not isinstance(value, types.ModuleType)
        }

    def record(frame, kind: str, name: str, extra: dict | None = None) -> None:
        if len(events) >= MAX_EVENTS:
            state["truncated"] = True
            return
        key = id(frame)
        number = frame_ids.setdefault(key, len(frame_ids) + 1)
        current = snapshot(frame)
        previous = last_values.get(key, {})
        changed = {
            var: value for var, value in current.items() if previous.get(var) != value
        }
        last_values[key] = current

        # What changed was done by the line before, in this frame.
        if changed and key in last_line:
            events[last_line[key]].setdefault("defined", {}).update(changed)

        event = {
            "i": len(events),
            "kind": kind,
            "file": name,
            "line": frame.f_lineno,
            "function": frame.f_code.co_name,
            "frame": number,
        }
        if kind == "call":
            event["defined"] = changed
            back = frame.f_back
            if back is not None and id(back) in frame_ids:
                event["caller"] = frame_ids[id(back)]
        if extra:
            event.update(extra)
        events.append(event)
        if kind in ("call", "line"):
            last_line[key] = event["i"]

    def local(frame, kind, arg):
        name = relative(frame.f_code.co_filename)
        if name is None:
            return None
        if kind == "line":
            record(frame, "line", name)
        elif kind == "return":
            record(frame, "return", name, {"value": short(arg)})
            last_line.pop(id(frame), None)
            last_values.pop(id(frame), None)
        elif kind == "exception":
            error = arg[1]
            record(
                frame,
                "exception",
                name,
                {
                    "type": type(error).__name__,
                    "message": str(error)[:300],
                    "error": id(error),
                },
            )
            # In a test, the exception reaching the test function is the failure.
            if frame.f_code.co_name.startswith("test"):
                state["failing"] = id(error)
        return local

    def tracer(frame, kind, arg):
        if kind != "call":
            return None
        name = relative(frame.f_code.co_filename)
        if name is None:
            return None
        record(frame, "call", name)
        return local

    outcome: dict = {}
    if mode == "pytest":
        import pytest  # before tracing starts: its import is not the program

    sys.settrace(tracer)
    threading.settrace(tracer)
    try:
        if mode == "pytest":
            outcome["exit_code"] = int(
                pytest.main(
                    [target, "-q", "-x", "-p", "no:cacheprovider", "--no-header"]
                )
            )
        else:
            sys.argv = [target]
            runpy.run_path(target, run_name="__main__")
            outcome["exit_code"] = 0
    except SystemExit as stop:
        outcome["exit_code"] = stop.code if isinstance(stop.code, int) else 1
    except BaseException as error:  # noqa: BLE001 - the program's failure is the result
        outcome["exit_code"] = 1
        state["failing"] = id(error)
        outcome["exception"] = {
            "type": type(error).__name__,
            "message": str(error)[:300],
        }
    finally:
        sys.settrace(None)
        threading.settrace(None)  # type: ignore[arg-type]

    failing = state["failing"]
    if failing is not None:
        raised = next(
            (event for event in events if event.get("error") == failing), None
        )
        if raised is not None:
            outcome["raised_at"] = raised["i"]
            outcome.setdefault(
                "exception", {"type": raised["type"], "message": raised["message"]}
            )

    with open(out, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "root": root,
                "mode": mode,
                "target": target,
                "outcome": outcome,
                "truncated": state["truncated"],
                "events": events,
            },
            handle,
        )


if __name__ == "__main__":
    main()
