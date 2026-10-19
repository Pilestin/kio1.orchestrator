"""End-to-end tests: `main()` with a scripted Ollama response and scripted input."""

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import main as main_module

SAMPLE_PLAN: dict[str, Any] = {
    "workflow_id": "wf-e2e01",
    "execution_mode": "mixed",
    "steps": [
        {"step_id": "s1", "agent_id": "KIO10", "capability": "tinyml", "task": "a"},
        {
            "step_id": "s2",
            "agent_id": "KIO12",
            "capability": "cybersecurity_validation",
            "task": "b",
        },
    ],
    "explanation": "e2e",
}


@pytest.fixture(autouse=True)
def restore_root_logger() -> Iterator[None]:
    """Undo the root logger reconfiguration done by `main()`.

    Returns:
        An iterator that yields once, restoring handlers on teardown.
    """
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level

    yield

    for handler in root.handlers[:]:
        if handler not in original_handlers:
            handler.close()
    root.handlers = original_handlers
    root.setLevel(original_level)


def _mock_ollama_response(content: str) -> MagicMock:
    """Build a mocked `urlopen` context manager returning an Ollama chat reply.

    Args:
        content: The assistant message content Ollama should appear to return.

    Returns:
        A mock usable as `urlopen`'s return value.
    """
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps(
        {"message": {"content": content}}
    ).encode("utf-8")
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)
    return mock_response


def write_config(tmp_path: Path) -> None:
    """Write a minimal config.json and prompt.txt into the scratch directory.

    Args:
        tmp_path: Scratch directory used as the working directory.

    Returns:
        None.
    """
    (tmp_path / "prompt.txt").write_text("You are KIO1.")
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "model": "test-model",
                "prompt_path": "prompt.txt",
                "chat_directory": "chats",
                "temperature": 0.1,
                "request_timeout": 5,
                "endpoint": "http://localhost:11434",
                "context_window_size": 16384,
            }
        )
    )


def run_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    inputs: list[str],
) -> None:
    """Run `main()` once in a scratch directory with scripted input.

    Args:
        tmp_path: Scratch directory used as the working directory.
        monkeypatch: Fixture for redirecting input and cwd.
        inputs: Queries typed at the `>` prompt, in order.

    Returns:
        None.
    """
    write_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    queries = iter(inputs)
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(queries))

    main_module.main()


class TestMainEndToEnd:
    """End-to-end tests driving `main()` with a mocked Ollama endpoint."""

    @patch("main.urllib.request.urlopen")
    def test_prints_formatted_json_response(
        self,
        mock_urlopen: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Verify the model's JSON reply is printed pretty-printed and unchanged."""
        mock_urlopen.return_value = _mock_ollama_response(json.dumps(SAMPLE_PLAN))

        run_main(tmp_path, monkeypatch, ["build battery firmware", "exit"])
        out = capsys.readouterr().out

        printed_plan = out[out.index("{") : out.rindex("}") + 1]
        assert json.loads(printed_plan) == SAMPLE_PLAN
        assert "Goodbye." in out

    @patch("main.urllib.request.urlopen")
    def test_chat_history_is_persisted(
        self,
        mock_urlopen: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Verify the system/user/assistant turn is appended to the chat file."""
        mock_urlopen.return_value = _mock_ollama_response(json.dumps(SAMPLE_PLAN))

        run_main(tmp_path, monkeypatch, ["build battery firmware", "exit"])

        chat_lines = (
            next((tmp_path / "chats").glob("chat_*.jsonl")).read_text().splitlines()
        )
        roles = [json.loads(line)["role"] for line in chat_lines]
        assert roles == ["system", "user", "assistant"]
        assert json.loads(chat_lines[1])["content"] == "build battery firmware"
        assert json.loads(json.loads(chat_lines[2])["content"]) == SAMPLE_PLAN

    @patch("main.urllib.request.urlopen")
    def test_continues_accepting_queries_after_response(
        self,
        mock_urlopen: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Verify the REPL loops back for another query after printing a response."""
        mock_urlopen.return_value = _mock_ollama_response(json.dumps(SAMPLE_PLAN))

        run_main(tmp_path, monkeypatch, ["first query", "second query", "exit"])
        out = capsys.readouterr().out

        assert out.count(json.dumps(SAMPLE_PLAN, indent=2)) == 2
        chat_lines = (
            next((tmp_path / "chats").glob("chat_*.jsonl")).read_text().splitlines()
        )
        assert [json.loads(line)["role"] for line in chat_lines] == [
            "system",
            "user",
            "assistant",
            "user",
            "assistant",
        ]

    @patch("main.urllib.request.urlopen")
    def test_invalid_json_response_reports_error_and_continues(
        self,
        mock_urlopen: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Verify a non-JSON reply is reported as an error without crashing the loop."""
        mock_urlopen.return_value = _mock_ollama_response("not valid json")

        run_main(tmp_path, monkeypatch, ["build battery firmware", "exit"])
        captured = capsys.readouterr()

        assert "Error:" in captured.err
        assert "Goodbye." in captured.out

    @patch("main.urllib.request.urlopen")
    def test_blank_query_is_ignored(
        self,
        mock_urlopen: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Verify a blank line does not count as a turn and does not call Ollama."""
        mock_urlopen.return_value = _mock_ollama_response(json.dumps(SAMPLE_PLAN))

        run_main(tmp_path, monkeypatch, ["   ", "exit"])

        # Only the preload call should have reached `urlopen`.
        assert mock_urlopen.call_count == 1

    @patch("main.urllib.request.urlopen")
    def test_turn_completed_is_logged_with_duration(
        self,
        mock_urlopen: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify a successful turn logs its total and per-stage durations.

        `init_logger()` replaces the root logger's handlers (including
        pytest's `caplog` handler), so the session log file written to disk
        is read directly instead of relying on `caplog`.
        """
        mock_urlopen.return_value = _mock_ollama_response(json.dumps(SAMPLE_PLAN))

        run_main(tmp_path, monkeypatch, ["build battery firmware", "exit"])

        log_text = next((tmp_path / "logs").glob("log_*.log")).read_text()
        assert "Turn completed: turn=1 duration_ms=" in log_text
        assert "History loaded: turn=1 duration_ms=" in log_text
        assert "Response formatted: turn=1 duration_ms=" in log_text
        assert "History persisted: turn=1 duration_ms=" in log_text
        assert "Session ended: turns=1 duration_ms=" in log_text

    @patch("main.urllib.request.urlopen")
    def test_turn_failed_is_logged_with_duration(
        self,
        mock_urlopen: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Verify a failed turn logs its duration alongside the traceback."""
        mock_urlopen.return_value = _mock_ollama_response("not valid json")

        run_main(tmp_path, monkeypatch, ["build battery firmware", "exit"])

        assert "Turn failed: turn=1 duration_ms=" in capsys.readouterr().err
