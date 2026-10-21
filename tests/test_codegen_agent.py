"""Tests for the agent that really writes code.

Its input is whatever a language model returned, which makes most of what
follows about not trusting it: where it may write, how much, and what happens
when the answer is not what was asked for.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from core.config_loader import Config
from tools.codegen_agent import (
    MAX_FILE_BYTES,
    GenerationError,
    generate,
    resolve_within,
)


def config() -> Config:
    """Return a configuration the provider fake ignores."""
    return Config(
        provider="ollama",
        allowed_providers=frozenset({"ollama"}),
        model="test-model",
        prompt_path="prompts/test.txt",
        chat_directory="chats",
        temperature=0.2,
        request_timeout=30,
        keep_alive=-1,
        max_output_tokens=1024,
    )


class Model:
    """Answers with whatever the test decided the model would say."""

    def __init__(self, content: Any) -> None:
        self.content = content if isinstance(content, str) else json.dumps(content)
        self.tasks: list[str] = []

    def send_request(self, **kwargs: Any) -> dict[str, Any]:
        self.tasks.append(kwargs["messages"][-1]["content"])

        return {"content": self.content}

    @staticmethod
    def extract_content(response: dict[str, Any]) -> str:
        return str(response["content"])


def run(model: Model, root: Path, task: str = "Build a thing") -> dict[str, Any]:
    """Generate with a fake model and return the output."""
    return generate(task, root, config(), model, None)


# ── where it may write ──────────────────────────────────────────────────────


def test_a_relative_path_lands_inside_the_workspace(tmp_path: Path) -> None:
    assert resolve_within(tmp_path, "app/main.py") == (tmp_path / "app/main.py")


def test_a_path_climbing_out_is_refused(tmp_path: Path) -> None:
    # The one that matters: a model asking to write outside its workspace.
    with pytest.raises(GenerationError, match="outside the workspace"):
        resolve_within(tmp_path, "../../.ssh/authorized_keys")


def test_a_deeply_nested_escape_is_refused(tmp_path: Path) -> None:
    with pytest.raises(GenerationError, match="outside the workspace"):
        resolve_within(tmp_path, "app/../../../etc/passwd")


def test_an_absolute_path_is_taken_as_relative(tmp_path: Path) -> None:
    # A leading slash is treated as workspace-relative rather than refused:
    # models write "/main.py" meaning the project root often enough that
    # failing on it would be unhelpful, and it cannot escape either way.
    assert resolve_within(tmp_path, "/main.py") == (tmp_path / "main.py")


def test_a_windows_separator_is_understood(tmp_path: Path) -> None:
    assert resolve_within(tmp_path, "app\\main.py") == (tmp_path / "app/main.py")


def test_an_empty_path_is_refused(tmp_path: Path) -> None:
    with pytest.raises(GenerationError, match="no path"):
        resolve_within(tmp_path, "   ")


def test_nothing_is_written_outside_the_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("untouched", encoding="utf-8")

    model = Model({"files": [{"path": "../secret.txt", "content": "overwritten"}]})

    with pytest.raises(GenerationError, match="outside the workspace"):
        run(model, workspace)

    assert outside.read_text(encoding="utf-8") == "untouched"


# ── what it produces ────────────────────────────────────────────────────────


def test_files_really_reach_the_disk(tmp_path: Path) -> None:
    model = Model(
        {
            "files": [
                {"path": "main.py", "content": "print('hello')\n"},
                {"path": "app/api.py", "content": "ROUTES = []\n"},
            ],
            "notes": "A tiny thing.",
        }
    )

    output = run(model, tmp_path)

    assert (tmp_path / "main.py").read_text(encoding="utf-8") == "print('hello')\n"
    assert (tmp_path / "app/api.py").exists()
    assert [item["path"] for item in output["files"]] == ["main.py", "app/api.py"]
    assert output["notes"] == "A tiny thing."


def test_the_task_reaches_the_model_unchanged(tmp_path: Path) -> None:
    model = Model({"files": [{"path": "a.py", "content": "x = 1\n"}]})

    run(model, tmp_path, task="Write a CRUD API for /items")

    assert model.tasks == ["Write a CRUD API for /items"]


def test_it_asks_for_a_human_to_look(tmp_path: Path) -> None:
    # Nothing here compiled or ran what the model wrote, and a `clean` verdict
    # on its own would read as more assurance than there is.
    model = Model({"files": [{"path": "a.py", "content": "x = 1\n"}]})

    assert run(model, tmp_path)["hitl_required"] is True


def test_a_fenced_answer_is_still_read(tmp_path: Path) -> None:
    # Small models wrap JSON in Markdown however firmly they are told not to.
    model = Model('```json\n{"files": [{"path": "a.py", "content": "x = 1\\n"}]}\n```')

    assert run(model, tmp_path)["files"][0]["path"] == "a.py"


# ── when the answer is not usable ───────────────────────────────────────────


def test_prose_instead_of_json_is_reported(tmp_path: Path) -> None:
    model = Model("Certainly! Here is how you would build that:")

    with pytest.raises(GenerationError, match="did not return usable JSON"):
        run(model, tmp_path)


def test_an_answer_without_files_is_reported(tmp_path: Path) -> None:
    model = Model({"summary": "I thought about it."})

    with pytest.raises(GenerationError, match="no 'files' list"):
        run(model, tmp_path)


def test_an_empty_file_list_is_reported(tmp_path: Path) -> None:
    model = Model({"files": []})

    with pytest.raises(GenerationError, match="no usable files"):
        run(model, tmp_path)


def test_a_file_with_no_content_is_reported(tmp_path: Path) -> None:
    model = Model({"files": [{"path": "a.py"}]})

    with pytest.raises(GenerationError, match="without string content"):
        run(model, tmp_path)


def test_an_implausibly_large_file_is_refused(tmp_path: Path) -> None:
    model = Model({"files": [{"path": "a.py", "content": "x" * (MAX_FILE_BYTES + 1)}]})

    with pytest.raises(GenerationError, match="implausibly large"):
        run(model, tmp_path)

    assert not (tmp_path / "a.py").exists()


def test_too_many_files_are_capped(tmp_path: Path) -> None:
    model = Model(
        {
            "files": [
                {"path": f"file{index}.py", "content": "x = 1\n"} for index in range(60)
            ]
        }
    )

    written = run(model, tmp_path)["files"]

    assert len(written) == 25
    assert len(list(tmp_path.glob("*.py"))) == 25
