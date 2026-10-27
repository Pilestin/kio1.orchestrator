"""Arguments that take their value from an earlier step's output.

A plan is written before anything runs, so a step whose input is another
step's output — the coordinates a geocoder finds, then the weather there —
cannot carry the value itself. It carries a reference instead:

    {"latitude": "${s1.latitude}", "longitude": "${s1.longitude}"}

and the value is filled in when `s1` has answered, just before the step is
sent. A KIO agent has always had its dependencies' outputs in
`data.upstream`; this is the same thing for targets that take named
arguments — HTTP APIs and MCP tools — which read nothing else.

The planner sees what a target takes but not what it returns, so it guesses
the path. When the path as written is not there, the first field anywhere in
the output with the reference's last name is used: `${s1.latitude}` finds
`results[0].latitude`. A reference that still finds nothing is left as it
is, and the step fails saying what it was sent, rather than being sent an
invented value.
"""

import json
import re
from typing import Any

# `${s1}`, `${s1.latitude}`, `${r2-s1.results.0.name}`. Only names of steps in
# the plan count: `${HOME}` in a tool's own definition is an environment
# variable, resolved by its transport, and never reaches this.
REFERENCE = re.compile(r"\$\{([A-Za-z][\w-]*)((?:\.[\w-]+)*)\}")


def references_in(value: Any) -> list[tuple[str, str]]:
    """Return every (step id, path) a value refers to, nested values included."""
    found: list[tuple[str, str]] = []

    if isinstance(value, str):
        found.extend(
            (match.group(1), match.group(2).lstrip("."))
            for match in REFERENCE.finditer(value)
        )
    elif isinstance(value, dict):
        for item in value.values():
            found.extend(references_in(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(references_in(item))

    return found


def referenced_steps(arguments: dict[str, Any], step_ids: set[str]) -> list[str]:
    """Return the plan's steps a step's arguments refer to, in order, once each.

    Args:
        arguments: The step's arguments.
        step_ids: Every step id in the plan; anything else is not a step.
    """
    steps: list[str] = []

    for step_id, _ in references_in(arguments):
        if step_id in step_ids and step_id not in steps:
            steps.append(step_id)

    return steps


def rename_references(value: Any, renamed: dict[str, str]) -> Any:
    """Rewrite references to renamed steps, as a follow-up round's `r2-` prefix."""
    if isinstance(value, str):
        return REFERENCE.sub(
            lambda match: (
                "${" + renamed[match.group(1)] + match.group(2) + "}"
                if match.group(1) in renamed
                else match.group(0)
            ),
            value,
        )
    if isinstance(value, dict):
        return {key: rename_references(item, renamed) for key, item in value.items()}
    if isinstance(value, list):
        return [rename_references(item, renamed) for item in value]

    return value


_MISSING = object()


def _follow(output: Any, path: list[str]) -> Any:
    """Walk a path of keys and list indexes; `_MISSING` when it leads nowhere."""
    current = output

    for part in path:
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return _MISSING

    return current


def _find_key(output: Any, key: str) -> Any:
    """The first value stored under `key` anywhere in the output, breadth first."""
    queue = [output]

    while queue:
        current = queue.pop(0)

        if isinstance(current, dict):
            if key in current:
                return current[key]
            queue.extend(current.values())
        elif isinstance(current, list):
            queue.extend(current)

    return _MISSING


def lookup(output: Any, path: str) -> Any:
    """Return the value a path names in an output, or `_MISSING`.

    The path as written first; when it is not there, its last name anywhere.
    """
    if not path:
        return output

    parts = path.split(".")
    found = _follow(output, parts)

    if found is _MISSING and not parts[-1].isdigit():
        found = _find_key(output, parts[-1])

    return found


def resolve(value: Any, outputs: dict[str, Any]) -> tuple[Any, list[str]]:
    """Fill references in a value from the outputs of steps that have run.

    A string that is nothing but one reference becomes the value itself, so a
    number stays a number; a reference inside longer text is written into it.

    Args:
        value: An argument value, or a structure of them.
        outputs: Step id to that step's output.

    Returns:
        The value with what could be filled filled, and the references that
        could not be, as written.
    """
    unresolved: list[str] = []

    def fill(item: Any) -> Any:
        if isinstance(item, dict):
            return {key: fill(inner) for key, inner in item.items()}
        if isinstance(item, list):
            return [fill(inner) for inner in item]
        if not isinstance(item, str):
            return item

        whole = REFERENCE.fullmatch(item.strip())

        if whole and whole.group(1) in outputs:
            found = lookup(outputs[whole.group(1)], whole.group(2).lstrip("."))
            if found is not _MISSING:
                return found

        def substitute(match: re.Match[str]) -> str:
            if match.group(1) not in outputs:
                return match.group(0)

            found = lookup(outputs[match.group(1)], match.group(2).lstrip("."))

            if found is _MISSING:
                unresolved.append(match.group(0))
                return match.group(0)

            return (
                found
                if isinstance(found, str)
                else json.dumps(found, ensure_ascii=False)
            )

        return REFERENCE.sub(substitute, item)

    return fill(value), unresolved
