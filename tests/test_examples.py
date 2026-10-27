"""Tests for the example gallery on How to use → Examples.

Every example has a "Put this in the form" button, so one that the registry
refuses is a button that leads to an error. These load each example exactly
as saving it would, without calling anything.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from dispatch.agent_registry import load_agent_entry

EXAMPLES_FILE = Path(__file__).resolve().parent.parent / "static" / "examples.json"
EXAMPLES: list[dict[str, Any]] = json.loads(EXAMPLES_FILE.read_text(encoding="utf-8"))


def test_every_example_has_what_the_gallery_shows() -> None:
    for example in EXAMPLES:
        assert {"id", "group", "title", "summary", "entry"} <= set(example), example.get("id")


def test_example_ids_are_unique() -> None:
    ids = [example["id"] for example in EXAMPLES]

    assert len(ids) == len(set(ids))


def test_example_target_names_are_unique() -> None:
    names = [example["entry"]["agent_id"] for example in EXAMPLES]

    assert len(names) == len(set(names))


@pytest.mark.parametrize("example", EXAMPLES, ids=[example["id"] for example in EXAMPLES])
def test_each_example_is_a_definition_the_registry_accepts(example: dict[str, Any]) -> None:
    entry = example["entry"]
    endpoint = load_agent_entry(entry["agent_id"], entry)

    assert endpoint.kind == entry.get("type", "kio")
