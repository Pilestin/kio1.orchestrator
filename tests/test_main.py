"""End-to-end tests: `main()` startup — config load, preload, and web server launch."""

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import main as main_module


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


class TestMainStartup:
    """Tests driving `main()` up to (but not including) the blocking web server."""

    @patch("flask.Flask.run")
    @patch("main.urllib.request.urlopen")
    def test_preloads_model_and_starts_web_server(
        self,
        mock_urlopen: MagicMock,
        mock_run: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Verify the model is preloaded and the Flask app is started."""
        mock_urlopen.return_value = _mock_ollama_response("")
        write_config(tmp_path)
        monkeypatch.chdir(tmp_path)

        main_module.main()

        out = capsys.readouterr().out
        assert "Loading Ollama model test-model..." in out
        assert "KIO1 Orchestrator web UI: http://127.0.0.1:5000" in out
        mock_run.assert_called_once()
        _, run_kwargs = mock_run.call_args
        assert run_kwargs["host"] == "127.0.0.1"
        assert run_kwargs["port"] == 5000
        assert run_kwargs["debug"] is False

    @patch("flask.Flask.run")
    @patch("main.urllib.request.urlopen")
    def test_web_host_and_port_env_overrides(
        self,
        mock_urlopen: MagicMock,
        mock_run: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify KIO1_WEB_HOST and KIO1_WEB_PORT override the server bind address."""
        mock_urlopen.return_value = _mock_ollama_response("")
        write_config(tmp_path)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("KIO1_WEB_HOST", "0.0.0.0")
        monkeypatch.setenv("KIO1_WEB_PORT", "8080")

        main_module.main()

        _, run_kwargs = mock_run.call_args
        assert run_kwargs["host"] == "0.0.0.0"
        assert run_kwargs["port"] == 8080

    @patch("flask.Flask.run")
    @patch("main.urllib.request.urlopen")
    def test_chat_file_is_created_before_server_starts(
        self,
        mock_urlopen: MagicMock,
        mock_run: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify a chat history file with the system prompt exists after startup."""
        mock_urlopen.return_value = _mock_ollama_response("")
        write_config(tmp_path)
        monkeypatch.chdir(tmp_path)

        main_module.main()

        chat_file = next((tmp_path / "chats").glob("chat_*.jsonl"))
        first_line = json.loads(chat_file.read_text().splitlines()[0])
        assert first_line == {"role": "system", "content": "You are KIO1."}

    def test_startup_failure_reports_error_and_exits(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Verify a missing config.json is reported and exits with status 1."""
        monkeypatch.chdir(tmp_path)

        with pytest.raises(SystemExit) as exc_info:
            main_module.main()

        assert exc_info.value.code == 1
        assert "Error: failed to start orchestrator" in capsys.readouterr().err

    @patch("flask.Flask.run")
    @patch("main.urllib.request.urlopen")
    def test_session_ended_is_logged_after_server_stops(
        self,
        mock_urlopen: MagicMock,
        mock_run: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify session end is logged once the (mocked) server returns."""
        mock_urlopen.return_value = _mock_ollama_response("")
        write_config(tmp_path)
        monkeypatch.chdir(tmp_path)

        main_module.main()

        log_text = next((tmp_path / "logs").glob("log_*.log")).read_text()
        assert "Session ended: duration_ms=" in log_text
