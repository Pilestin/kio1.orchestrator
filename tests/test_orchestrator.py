import json
import logging
import re
from collections.abc import Iterator
from formatter import format_json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from chat_history import (
    append_assistant_message,
    append_user_message,
    create_chat_file,
    load_messages,
)
from config_loader import Config, TelemetryConfig, load_config
from main import extract_content, preload_model, send_request
from prompt_loader import load_prompt
from session_logger import generate_session_id, init_logger


def make_config(
    model: str = "test-model",
    temperature: float = 0.1,
    request_timeout: float = 120,
    keep_alive: int = -1,
    max_output_tokens: int = 1024,
    endpoint: str = "http://localhost:11434",
    context_window_size: int = 16384,
) -> Config:
    """Create a configuration for Ollama request tests.

    Args:
        model: Model identifier.
        temperature: Sampling temperature.
        request_timeout: Request timeout in seconds.
        keep_alive: Ollama keep_alive value in seconds, or -1 to keep loaded.
        max_output_tokens: Maximum number of generated output tokens.
        endpoint: Ollama base URL.
        context_window_size: Ollama context window in tokens.

    Returns:
        A `Config` instance for use in tests.
    """
    return Config(
        model=model,
        prompt_path="prompt.txt",
        chat_directory="chats",
        temperature=temperature,
        request_timeout=request_timeout,
        keep_alive=keep_alive,
        max_output_tokens=max_output_tokens,
        endpoint=endpoint,
        context_window_size=context_window_size,
    )


class TestConfigLoader:
    """Tests for configuration loading."""

    def test_load_config_logs_duration(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Verify config loading logs its duration alongside the loaded fields."""
        config_data = {
            "model": "test-model",
            "prompt_path": "prompts/test.txt",
            "chat_directory": "chats",
            "temperature": 0.1,
            "request_timeout": 120,
            "max_output_tokens": 2048,
            "endpoint": "http://localhost:11434",
            "context_window_size": 16384,
        }
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(config_data))

        with caplog.at_level(logging.INFO, logger="config_loader"):
            load_config(str(config_file))

        assert "Config loaded:" in caplog.text
        assert "model=test-model" in caplog.text
        assert "duration_ms=" in caplog.text

    def test_load_config_reads_all_fields(self, tmp_path: Path) -> None:
        """Verify all config fields are loaded correctly.

        Args:
            tmp_path: Pytest temporary directory fixture.

        Returns:
            None.
        """
        config_data = {
            "model": "test-model",
            "prompt_path": "prompts/test.txt",
            "chat_directory": "chats",
            "temperature": 0.1,
            "request_timeout": 120,
            "max_output_tokens": 2048,
            "endpoint": "http://localhost:11434",
            "context_window_size": 16384,
            "telemetry": {
                "enabled": True,
                "service_name": "test-orchestrator",
                "otlp_http_endpoint": "http://collector:4318/",
                "otlp_bearer_token": "test-token",
                "metric_export_interval_ms": 2500,
                "trace_sample_ratio": 0.5,
            },
        }
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(config_data))

        config = load_config(str(config_file))

        assert config.model == "test-model"
        assert config.prompt_path == "prompts/test.txt"
        assert config.chat_directory == "chats"
        assert config.temperature == 0.1
        assert config.request_timeout == 120
        assert config.keep_alive == -1
        assert config.max_output_tokens == 2048
        assert config.endpoint == "http://localhost:11434"
        assert config.context_window_size == 16384
        assert config.telemetry == TelemetryConfig(
            enabled=True,
            service_name="test-orchestrator",
            otlp_http_endpoint="http://collector:4318",
            otlp_bearer_token="test-token",
            metric_export_interval_ms=2500,
            trace_sample_ratio=0.5,
        )

    def test_load_config_uses_telemetry_defaults(self, tmp_path: Path) -> None:
        """Verify telemetry is safely disabled when its section is omitted.

        Args:
            tmp_path: Pytest temporary directory fixture.
        """
        config_data = {
            "model": "test-model",
            "prompt_path": "prompts/test.txt",
            "chat_directory": "chats",
            "temperature": 0.1,
            "request_timeout": 120,
            "max_output_tokens": 2048,
            "endpoint": "http://localhost:11434",
            "context_window_size": 16384,
        }
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(config_data))

        config = load_config(str(config_file))

        assert config.telemetry == TelemetryConfig()

    def test_load_config_normalizes_endpoint_trailing_slash(
        self, tmp_path: Path
    ) -> None:
        """Verify a trailing slash is stripped from the configured endpoint."""
        config_data = {
            "model": "test-model",
            "prompt_path": "prompts/test.txt",
            "chat_directory": "chats",
            "temperature": 0.1,
            "request_timeout": 120,
            "max_output_tokens": 2048,
            "endpoint": "http://localhost:11434/",
            "context_window_size": 16384,
        }
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(config_data))

        config = load_config(str(config_file))

        assert config.endpoint == "http://localhost:11434"

    def test_load_config_applies_ollama_endpoint_env_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify KIO1_OLLAMA_ENDPOINT overrides the configured endpoint."""
        config_data = {
            "model": "test-model",
            "prompt_path": "prompts/test.txt",
            "chat_directory": "chats",
            "temperature": 0.1,
            "request_timeout": 120,
            "max_output_tokens": 2048,
            "endpoint": "http://localhost:11434",
            "context_window_size": 16384,
        }
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(config_data))
        monkeypatch.setenv("KIO1_OLLAMA_ENDPOINT", "http://host.docker.internal:11434")

        config = load_config(str(config_file))

        assert config.endpoint == "http://host.docker.internal:11434"

    @pytest.mark.parametrize("invalid_endpoint", [None, "", "   ", 123])
    def test_load_config_rejects_invalid_endpoint(
        self, tmp_path: Path, invalid_endpoint: object
    ) -> None:
        """Verify a missing or non-string endpoint is rejected."""
        config_data = {
            "model": "test-model",
            "prompt_path": "prompts/test.txt",
            "chat_directory": "chats",
            "temperature": 0.1,
            "request_timeout": 120,
            "max_output_tokens": 2048,
            "endpoint": invalid_endpoint,
            "context_window_size": 16384,
        }
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(config_data))

        with pytest.raises(ValueError, match="endpoint"):
            load_config(str(config_file))

    @pytest.mark.parametrize(
        "invalid_context_window_size", [None, 0, -1, "8192", 1.5, True]
    )
    def test_load_config_rejects_invalid_context_window_size(
        self, tmp_path: Path, invalid_context_window_size: object
    ) -> None:
        """Verify a missing or non-positive-integer context_window_size is rejected."""
        config_data = {
            "model": "test-model",
            "prompt_path": "prompts/test.txt",
            "chat_directory": "chats",
            "temperature": 0.1,
            "request_timeout": 120,
            "max_output_tokens": 2048,
            "endpoint": "http://localhost:11434",
            "context_window_size": invalid_context_window_size,
        }
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(config_data))

        with pytest.raises(ValueError, match="context_window_size"):
            load_config(str(config_file))

    def test_load_config_rejects_max_output_tokens_exceeding_context(
        self, tmp_path: Path
    ) -> None:
        """Verify max_output_tokens must leave room for the prompt."""
        config_data = {
            "model": "test-model",
            "prompt_path": "prompts/test.txt",
            "chat_directory": "chats",
            "temperature": 0.1,
            "request_timeout": 120,
            "max_output_tokens": 8192,
            "endpoint": "http://localhost:11434",
            "context_window_size": 8192,
        }
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(config_data))

        with pytest.raises(ValueError, match="must be smaller than"):
            load_config(str(config_file))

    def test_load_config_reads_optional_ollama_runtime_fields(
        self, tmp_path: Path
    ) -> None:
        """Verify optional Ollama runtime config fields are loaded.

        Args:
            tmp_path: Pytest temporary directory fixture.

        Returns:
            None.
        """
        config_data = {
            "model": "test-model",
            "prompt_path": "prompts/test.txt",
            "chat_directory": "chats",
            "temperature": 0.1,
            "request_timeout": 120,
            "keep_alive": 600,
            "max_output_tokens": 2048,
            "endpoint": "http://localhost:11434",
            "context_window_size": 16384,
        }
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(config_data))

        config = load_config(str(config_file))

        assert config.keep_alive == 600

    @pytest.mark.parametrize("invalid_keep_alive", [True, False, 1.5, "600", None])
    def test_load_config_rejects_non_integer_keep_alive(
        self, tmp_path: Path, invalid_keep_alive: object
    ) -> None:
        """Verify non-integer keep_alive values are rejected.

        Args:
            tmp_path: Pytest temporary directory fixture.

        Returns:
            None.
        """
        config_data = {
            "model": "test-model",
            "prompt_path": "prompts/test.txt",
            "chat_directory": "chats",
            "temperature": 0.1,
            "request_timeout": 120,
            "keep_alive": invalid_keep_alive,
            "endpoint": "http://localhost:11434",
            "context_window_size": 16384,
        }
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(config_data))

        with pytest.raises(ValueError, match="keep_alive must be an integer"):
            load_config(str(config_file))

    def test_load_config_missing_file_raises(self) -> None:
        """Verify that a missing config file raises an error.

        Args:
            None.

        Returns:
            None.
        """
        with pytest.raises(FileNotFoundError):
            load_config("/nonexistent/config.json")

    def test_config_dataclass_fields(self) -> None:
        """Verify Config dataclass holds the expected values.

        Args:
            None.

        Returns:
            None.
        """
        config = Config(
            model="m",
            prompt_path="p.txt",
            chat_directory="c",
            temperature=0.1,
            request_timeout=120,
            keep_alive=-1,
            max_output_tokens=2048,
            endpoint="http://localhost:11434",
            context_window_size=16384,
        )
        assert config.model == "m"
        assert config.temperature == 0.1
        assert config.request_timeout == 120
        assert config.keep_alive == -1
        assert config.max_output_tokens == 2048
        assert config.endpoint == "http://localhost:11434"
        assert config.context_window_size == 16384


class TestPromptLoader:
    """Tests for prompt loading."""

    def test_load_prompt_returns_content(self, tmp_path: Path) -> None:
        """Verify prompt content is loaded and stripped.

        Args:
            tmp_path: Pytest temporary directory fixture.

        Returns:
            None.
        """
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("  You are a helpful assistant.  \n")

        result = load_prompt(str(prompt_file))

        assert result == "You are a helpful assistant."

    def test_load_prompt_missing_file_raises(self) -> None:
        """Verify that a missing prompt file raises an error.

        Args:
            None.

        Returns:
            None.
        """
        with pytest.raises(FileNotFoundError):
            load_prompt("/nonexistent/prompt.txt")

    def test_load_prompt_logs_duration_and_size(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Verify prompt loading logs its duration and character count."""
        content = "You are a helpful assistant."
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text(content)

        with caplog.at_level(logging.INFO, logger="prompt_loader"):
            load_prompt(str(prompt_file))

        assert "Prompt loaded:" in caplog.text
        assert f"path={prompt_file}" in caplog.text
        assert "duration_ms=" in caplog.text
        assert f"chars={len(content)}" in caplog.text


class TestChatHistory:
    """Tests for chat history storage."""

    def test_create_chat_file_creates_directory(self, tmp_path: Path) -> None:
        """Verify chat directory is created if it doesn't exist.

        Args:
            tmp_path: Pytest temporary directory fixture.

        Returns:
            None.
        """
        chat_dir = tmp_path / "chats"
        chat_file = create_chat_file(str(chat_dir), "system prompt")

        assert chat_dir.exists()
        assert chat_file.exists()

    def test_create_chat_file_writes_system_prompt(self, tmp_path: Path) -> None:
        """Verify the system prompt is the first line of the chat file as JSONL.

        Args:
            tmp_path: Pytest temporary directory fixture.

        Returns:
            None.
        """
        chat_dir = tmp_path / "chats"
        chat_file = create_chat_file(str(chat_dir), "You are an AI.")

        lines = chat_file.read_text().strip().split("\n")
        first = json.loads(lines[0])
        assert first == {"role": "system", "content": "You are an AI."}

    def test_create_chat_file_has_jsonl_extension(self, tmp_path: Path) -> None:
        """Verify the chat file uses the .jsonl extension.

        Args:
            tmp_path: Pytest temporary directory fixture.

        Returns:
            None.
        """
        chat_dir = tmp_path / "chats"
        chat_file = create_chat_file(str(chat_dir), "system")

        assert chat_file.suffix == ".jsonl"

    def test_create_chat_file_unique_names(self, tmp_path: Path) -> None:
        """Verify consecutive calls produce unique filenames.

        Args:
            tmp_path: Pytest temporary directory fixture.

        Returns:
            None.
        """
        chat_dir = tmp_path / "chats"
        file_a = create_chat_file(str(chat_dir), "system")
        file_b = create_chat_file(str(chat_dir), "system")

        assert file_a.name != file_b.name

    def test_append_user_message(self, tmp_path: Path) -> None:
        """Verify user messages are appended as JSONL.

        Args:
            tmp_path: Pytest temporary directory fixture.

        Returns:
            None.
        """
        chat_dir = tmp_path / "chats"
        chat_file = create_chat_file(str(chat_dir), "system")
        append_user_message(chat_file, "hello")

        lines = chat_file.read_text().strip().split("\n")
        second = json.loads(lines[1])
        assert second == {"role": "user", "content": "hello"}

    def test_append_assistant_message(self, tmp_path: Path) -> None:
        """Verify assistant messages are appended as JSONL.

        Args:
            tmp_path: Pytest temporary directory fixture.

        Returns:
            None.
        """
        chat_dir = tmp_path / "chats"
        chat_file = create_chat_file(str(chat_dir), "system")
        append_user_message(chat_file, "hello")
        append_assistant_message(chat_file, '{"reply": "hi"}')

        lines = chat_file.read_text().strip().split("\n")
        third = json.loads(lines[2])
        assert third == {"role": "assistant", "content": '{"reply": "hi"}'}

    def test_load_messages_returns_correct_roles(self, tmp_path: Path) -> None:
        """Verify messages are loaded with alternating roles.

        Args:
            tmp_path: Pytest temporary directory fixture.

        Returns:
            None.
        """
        chat_dir = tmp_path / "chats"
        chat_file = create_chat_file(str(chat_dir), "system prompt")
        append_user_message(chat_file, "question 1")
        append_assistant_message(chat_file, "answer 1")
        append_user_message(chat_file, "question 2")

        messages = load_messages(chat_file)

        assert len(messages) == 3
        assert messages[0] == {"role": "user", "content": "question 1"}
        assert messages[1] == {"role": "assistant", "content": "answer 1"}
        assert messages[2] == {"role": "user", "content": "question 2"}

    def test_load_messages_empty_conversation(self, tmp_path: Path) -> None:
        """Verify empty conversation returns no messages.

        Args:
            tmp_path: Pytest temporary directory fixture.

        Returns:
            None.
        """
        chat_dir = tmp_path / "chats"
        chat_file = create_chat_file(str(chat_dir), "system prompt")

        messages = load_messages(chat_file)
        assert messages == []


class TestSessionLogger:
    """Tests for per-session logging setup."""

    @pytest.fixture(autouse=True)
    def restore_root_logger(self) -> Iterator[None]:
        """Save root logger state before each test and restore it afterwards.

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

    def test_generate_session_id_format(self) -> None:
        """Verify the session id is a timestamp followed by an 8-char hex suffix."""
        assert re.fullmatch(r"\d{8}_\d{6}_[0-9a-f]{8}", generate_session_id())

    def test_generate_session_id_is_unique(self) -> None:
        """Verify consecutive session ids differ even within the same second."""
        assert generate_session_id() != generate_session_id()

    def test_init_logger_creates_log_file(self, tmp_path: Path) -> None:
        """Verify the log file is created and named from the session id.

        Args:
            tmp_path: Pytest temporary directory fixture.
        """
        log_dir = tmp_path / "logs"
        init_logger(str(log_dir), "20260723_140211_a1b2c3d4")

        assert log_dir.exists()
        assert (log_dir / "log_20260723_140211_a1b2c3d4.log").exists()

    def test_init_logger_handler_levels(self, tmp_path: Path) -> None:
        """Verify the file handler captures DEBUG and the stream handler only ERROR.

        Args:
            tmp_path: Pytest temporary directory fixture.
        """
        init_logger(str(tmp_path / "logs"), "session1")
        handlers = logging.getLogger().handlers

        assert len(handlers) == 2
        file_handler, stream_handler = handlers
        assert isinstance(file_handler, logging.FileHandler)
        assert file_handler.level == logging.DEBUG
        assert stream_handler.level == logging.ERROR

    def test_init_logger_replaces_existing_handlers(self, tmp_path: Path) -> None:
        """Verify repeated initialization does not accumulate duplicate handlers.

        Args:
            tmp_path: Pytest temporary directory fixture.
        """
        init_logger(str(tmp_path / "logs"), "session1")
        init_logger(str(tmp_path / "logs"), "session2")

        assert len(logging.getLogger().handlers) == 2

    def test_file_handler_records_debug_messages(self, tmp_path: Path) -> None:
        """Verify DEBUG records reach the session log file.

        Args:
            tmp_path: Pytest temporary directory fixture.
        """
        log_dir = tmp_path / "logs"
        init_logger(str(log_dir), "session1")

        logging.getLogger("example").debug("debug detail")

        contents = (log_dir / "log_session1.log").read_text()
        assert "DEBUG example debug detail" in contents

    def test_stream_handler_emits_only_errors(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Verify INFO stays out of the console while ERROR reaches stderr.

        Args:
            tmp_path: Pytest temporary directory fixture.
            capsys: Pytest fixture capturing stdout and stderr.
        """
        init_logger(str(tmp_path / "logs"), "session1")

        logger = logging.getLogger("example")
        logger.info("info detail")
        logger.error("error detail")

        captured = capsys.readouterr()
        assert "info detail" not in captured.err
        assert "error detail" in captured.err

    def test_log_file_pairs_with_chat_file(self, tmp_path: Path) -> None:
        """Verify the log file and chat file share one session id.

        Args:
            tmp_path: Pytest temporary directory fixture.
        """
        session_id = generate_session_id()
        init_logger(str(tmp_path / "logs"), session_id)
        chat_file = create_chat_file(str(tmp_path / "chats"), "system", session_id)

        assert chat_file.name == f"chat_{session_id}.jsonl"
        assert (tmp_path / "logs" / f"log_{session_id}.log").exists()


class TestFormatter:
    """Tests for JSON formatting."""

    def test_format_json_pretty_prints(self) -> None:
        """Verify JSON is formatted with 2-space indentation."""
        raw = '{"key":"value","num":42}'
        result = format_json(raw)

        expected = '{\n  "key": "value",\n  "num": 42\n}'
        assert result == expected

    def test_format_json_nested(self) -> None:
        """Verify nested JSON is properly formatted."""
        raw = '{"outer":{"inner":"value"}}'
        result = format_json(raw)

        assert '"outer"' in result
        assert '"inner": "value"' in result

    def test_format_json_invalid_raises(self) -> None:
        """Verify invalid JSON raises an error.

        Args:
            None.

        Returns:
            None.
        """
        with pytest.raises((json.JSONDecodeError, ValueError, SyntaxError)):
            format_json("not valid json")

    def test_format_json_single_quotes_fallback(self) -> None:
        """Verify single-quoted Python dicts are handled via fallback.

        Args:
            None.

        Returns:
            None.
        """
        raw = "{'key': 'value', 'num': 42}"
        result = format_json(raw)

        assert '"key": "value"' in result
        assert '"num": 42' in result

    def test_format_json_removes_markdown_fence(self) -> None:
        """Verify fenced JSON is normalized before parsing.

        Args:
            None.

        Returns:
            None.
        """
        raw = """```json
        {"key": "value", "num": 42}
        ```"""

        result = format_json(raw)

        expected = '{\n  "key": "value",\n  "num": 42\n}'
        assert result == expected

    def test_format_json_raises_on_truncated_content(self) -> None:
        """Verify incomplete JSON raises ValueError, not SyntaxError."""
        truncated = '{"workflow_id": "wf-1", "explanation": "unterminated'

        with pytest.raises(ValueError, match="neither valid JSON"):
            format_json(truncated)


class TestMainOllamaRequests:
    """Tests for main.py's Ollama request/response handling with mocked HTTP."""

    @patch("main.urllib.request.urlopen")
    def test_send_request_builds_correct_payload(self, mock_urlopen: MagicMock) -> None:
        """Verify the request payload is correctly structured.

        Args:
            mock_urlopen: Mock for `urllib.request.urlopen`.

        Returns:
            None.
        """
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {"message": {"content": '{"result": "ok"}'}}
        ).encode("utf-8")
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        messages = [{"role": "user", "content": "test query"}]
        config = make_config(model="test-model")
        send_request(config, "system", messages)

        call_args = mock_urlopen.call_args[0][0]
        payload = json.loads(call_args.data.decode("utf-8"))

        assert payload["model"] == "test-model"
        assert payload["stream"] is False
        assert payload["format"] == "json"
        assert payload["keep_alive"] == -1
        assert payload["options"]["temperature"] == 0.1
        assert payload["messages"][0] == {"role": "system", "content": "system"}
        assert payload["messages"][1] == {"role": "user", "content": "test query"}

    @patch("main.urllib.request.urlopen")
    def test_send_request_returns_parsed_response(
        self, mock_urlopen: MagicMock
    ) -> None:
        """Verify the response is parsed as JSON.

        Args:
            mock_urlopen: Mock for `urllib.request.urlopen`.

        Returns:
            None.
        """
        expected = {"message": {"content": '{"workflow_id": "wf-1"}'}}
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(expected).encode("utf-8")
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        config = make_config(model="model")
        result = send_request(config, "sys", [])

        assert result == expected

    def test_extract_content_gets_message(self) -> None:
        """Verify content extraction from response dict.

        Args:
            None.

        Returns:
            None.
        """
        response = {"message": {"content": '{"key": "value"}'}}
        assert extract_content(response) == '{"key": "value"}'

    def test_extract_content_strips_whitespace(self) -> None:
        """Verify content extraction strips leading/trailing whitespace.

        Args:
            None.

        Returns:
            None.
        """
        response = {"message": {"content": '  {"key": "value"}  \n'}}
        assert extract_content(response) == '{"key": "value"}'

    @patch("main.urllib.request.urlopen")
    def test_send_request_uses_correct_url(self, mock_urlopen: MagicMock) -> None:
        """Verify the request URL includes the chat endpoint.

        Args:
            mock_urlopen: Mock for `urllib.request.urlopen`.

        Returns:
            None.
        """
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {"message": {"content": "{}"}}
        ).encode("utf-8")
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        config = make_config(model="model", endpoint="http://myhost:1234")
        send_request(config, "sys", [])

        call_args = mock_urlopen.call_args[0][0]
        assert call_args.full_url == "http://myhost:1234/api/chat"

    @patch("main.urllib.request.urlopen")
    def test_send_request_passes_timeout(self, mock_urlopen: MagicMock) -> None:
        """Verify the timeout parameter is forwarded to `urlopen`.

        Args:
            mock_urlopen: Mock for `urllib.request.urlopen`.

        Returns:
            None.
        """
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {"message": {"content": "{}"}}
        ).encode("utf-8")
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        config = make_config(model="model", request_timeout=60)
        send_request(config, "sys", [])

        assert mock_urlopen.call_args[1]["timeout"] == 60

    @patch("main.urllib.request.urlopen")
    def test_send_request_passes_custom_temperature(
        self, mock_urlopen: MagicMock
    ) -> None:
        """Verify a custom `temperature` is included in the payload options.

        Args:
            mock_urlopen: Mock for `urllib.request.urlopen`.

        Returns:
            None.
        """
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {"message": {"content": "{}"}}
        ).encode("utf-8")
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        config = make_config(model="model", temperature=0.5)
        send_request(config, "sys", [])

        call_args = mock_urlopen.call_args[0][0]
        payload = json.loads(call_args.data.decode("utf-8"))
        assert payload["options"]["temperature"] == 0.5

    @patch("main.urllib.request.urlopen")
    def test_send_request_passes_custom_keep_alive(
        self, mock_urlopen: MagicMock
    ) -> None:
        """Verify custom keep_alive value is included in payload.

        Args:
            mock_urlopen: Mock for `urllib.request.urlopen`.

        Returns:
            None.
        """
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {"message": {"content": "{}"}}
        ).encode("utf-8")
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        config = make_config(model="model", keep_alive=600)
        send_request(config, "sys", [])

        call_args = mock_urlopen.call_args[0][0]
        payload = json.loads(call_args.data.decode("utf-8"))
        assert payload["keep_alive"] == 600

    @patch("main.urllib.request.urlopen")
    def test_preload_model_sends_correct_payload(self, mock_urlopen: MagicMock) -> None:
        """Verify `preload_model` sends the expected payload to Ollama.

        Args:
            mock_urlopen: Mock for `urllib.request.urlopen`.

        Returns:
            None.
        """
        mock_response = MagicMock()
        mock_response.read.return_value = b""
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        config = make_config(model="test-model")
        preload_model(config)

        call_args = mock_urlopen.call_args[0][0]
        payload = json.loads(call_args.data.decode("utf-8"))

        assert call_args.full_url == "http://localhost:11434/api/chat"
        assert payload["model"] == "test-model"
        assert payload["messages"] == []
        assert payload["keep_alive"] == -1

    @patch("main.urllib.request.urlopen")
    def test_preload_model_passes_timeout(self, mock_urlopen: MagicMock) -> None:
        """Verify `preload_model` forwards the `timeout` parameter to `urlopen`.

        Args:
            mock_urlopen: Mock for `urllib.request.urlopen`.

        Returns:
            None.
        """
        mock_response = MagicMock()
        mock_response.read.return_value = b""
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        config = make_config(model="model", request_timeout=30)
        preload_model(config)

        assert mock_urlopen.call_args[1]["timeout"] == 30

    @patch("main.urllib.request.urlopen")
    def test_send_request_sends_context_options(self, mock_urlopen: MagicMock) -> None:
        """Verify num_ctx and num_predict are sent in the payload options."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {"message": {"content": "{}"}}
        ).encode("utf-8")
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        config = make_config(model="model", max_output_tokens=2048)
        send_request(config, "sys", [])

        payload = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
        assert payload["options"]["num_ctx"] == 16384
        assert payload["options"]["num_predict"] == 2048

    @patch("main.urllib.request.urlopen")
    def test_preload_sends_context_option(self, mock_urlopen: MagicMock) -> None:
        """Verify preload loads the model with the configured context size.

        Args:
            mock_urlopen: Mock for `urllib.request.urlopen`.
        """
        mock_response = MagicMock()
        mock_response.read.return_value = b""
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        config = make_config(model="model")
        preload_model(config)

        payload = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
        assert payload["options"]["num_ctx"] == 16384

    @patch("main.urllib.request.urlopen")
    def test_send_request_rejects_truncated_response(
        self, mock_urlopen: MagicMock
    ) -> None:
        """Verify a length-truncated response raises a clear error.

        Args:
            mock_urlopen: Mock for `urllib.request.urlopen`.
        """
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {
                "message": {"content": '{"workflow_id": "wf-1"'},
                "done_reason": "length",
                "prompt_eval_count": 3432,
                "eval_count": 664,
            }
        ).encode("utf-8")
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        config = make_config(model="model")

        with pytest.raises(ValueError, match="Response truncated"):
            send_request(config, "sys", [])


class TestMainOllamaLogging:
    """Tests for the INFO records emitted by main.py's Ollama calls."""

    @patch("main.urllib.request.urlopen")
    def test_send_request_logs_duration_and_tokens(
        self, mock_urlopen: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Verify the response line carries model, duration and token counts.

        Args:
            mock_urlopen: Mock for `urllib.request.urlopen`.
            caplog: Pytest fixture capturing log records.
        """
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {
                "message": {"content": "{}"},
                "prompt_eval_count": 412,
                "eval_count": 286,
            }
        ).encode("utf-8")
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        config = make_config(model="test-model")
        with caplog.at_level(logging.INFO, logger="main"):
            send_request(config, "sys", [])

        assert "Response received:" in caplog.text
        assert "model=test-model" in caplog.text
        assert "input_tokens=412" in caplog.text
        assert "output_tokens=286" in caplog.text
        assert "duration_ms=" in caplog.text
        assert "tokens_per_second=" in caplog.text

    @patch("main.urllib.request.urlopen")
    def test_preload_logs_duration(
        self, mock_urlopen: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Verify preload logs its own duration separately from requests.

        Args:
            mock_urlopen: Mock for `urllib.request.urlopen`.
            caplog: Pytest fixture capturing log records.
        """
        mock_response = MagicMock()
        mock_response.read.return_value = b""
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        config = make_config(model="test-model")
        with caplog.at_level(logging.INFO, logger="main"):
            preload_model(config)

        assert "Model preloaded:" in caplog.text
        assert "model=test-model" in caplog.text
        assert "duration_ms=" in caplog.text
        assert "Response received:" not in caplog.text

    @patch("main.urllib.request.urlopen")
    def test_send_request_logs_tokens_per_second_as_na_without_output_tokens(
        self, mock_urlopen: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Verify throughput falls back to n/a when eval_count is missing."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {"message": {"content": "{}"}}
        ).encode("utf-8")
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        config = make_config(model="test-model")
        with caplog.at_level(logging.INFO, logger="main"):
            send_request(config, "sys", [])

        assert "tokens_per_second=n/a" in caplog.text
