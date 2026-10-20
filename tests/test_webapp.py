"""Tests for the Flask web UI: chat turns, JSON formatting, and file uploads."""

import json
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from flask.testing import FlaskClient

import webapp
from chat_history import create_chat_file
from config_loader import Config
from session_logger import generate_session_id


def make_config(
    model: str = "test-model",
    temperature: float = 0.1,
    request_timeout: float = 5,
    keep_alive: int = -1,
    max_output_tokens: int = 1024,
    endpoint: str = "http://localhost:11434",
    context_window_size: int = 16384,
) -> Config:
    """Create a configuration for web UI tests.

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


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FlaskClient:
    """Build a Flask test client backed by a scratch chat file and assets dir."""
    monkeypatch.setattr(webapp, "ASSETS_DIRECTORY", tmp_path / "assets")

    session_id = generate_session_id()
    chat_file = create_chat_file(str(tmp_path / "chats"), "You are KIO1.", session_id)

    app = webapp.create_app(
        config=make_config(),
        system_prompt="You are KIO1.",
        chat_file=chat_file,
        session_id=session_id,
    )
    app.testing = True
    return app.test_client()


SAMPLE_PLAN = {
    "workflow_id": "wf-e2e01",
    "execution_mode": "mixed",
    "steps": [{"step_id": "s1", "agent_id": "KIO10", "task": "a"}],
    "explanation": "e2e",
}


class TestIndexRoute:
    """Tests for the page serving the chat UI."""

    def test_index_serves_html(self, client: FlaskClient) -> None:
        """Verify the root route returns the chat UI page."""
        response = client.get("/")

        assert response.status_code == 200
        assert b"<html" in response.data


class TestChatRoute:
    """Tests for `POST /api/chat`."""

    @patch("main.urllib.request.urlopen")
    def test_returns_formatted_json(
        self, mock_urlopen: MagicMock, client: FlaskClient
    ) -> None:
        """Verify the model's JSON reply comes back pretty-printed and unchanged."""
        mock_urlopen.return_value = _mock_ollama_response(json.dumps(SAMPLE_PLAN))

        response = client.post("/api/chat", json={"query": "build battery firmware"})

        assert response.status_code == 200
        data = response.get_json()
        assert json.loads(data["formatted"]) == SAMPLE_PLAN

    @patch("main.urllib.request.urlopen")
    def test_chat_history_is_persisted(
        self, mock_urlopen: MagicMock, client: FlaskClient, tmp_path: Path
    ) -> None:
        """Verify the user/assistant turn is appended to the chat file."""
        mock_urlopen.return_value = _mock_ollama_response(json.dumps(SAMPLE_PLAN))

        client.post("/api/chat", json={"query": "build battery firmware"})

        chat_lines = (
            next((tmp_path / "chats").glob("chat_*.jsonl")).read_text().splitlines()
        )
        roles = [json.loads(line)["role"] for line in chat_lines]
        assert roles == ["system", "user", "assistant"]
        assert json.loads(chat_lines[1])["content"] == "build battery firmware"
        assert json.loads(json.loads(chat_lines[2])["content"]) == SAMPLE_PLAN

    @patch("main.urllib.request.urlopen")
    def test_turn_count_increments_across_requests(
        self, mock_urlopen: MagicMock, client: FlaskClient, tmp_path: Path
    ) -> None:
        """Verify successive queries keep extending the same chat history."""
        mock_urlopen.return_value = _mock_ollama_response(json.dumps(SAMPLE_PLAN))

        client.post("/api/chat", json={"query": "first query"})
        client.post("/api/chat", json={"query": "second query"})

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
    def test_invalid_json_response_returns_raw_and_error(
        self, mock_urlopen: MagicMock, client: FlaskClient
    ) -> None:
        """Verify a non-JSON reply is reported as an error alongside the raw text."""
        mock_urlopen.return_value = _mock_ollama_response("not valid json")

        response = client.post("/api/chat", json={"query": "build battery firmware"})

        assert response.status_code == 200
        data = response.get_json()
        assert data["raw"] == "not valid json"
        assert "error" in data

    def test_blank_query_is_rejected(self, client: FlaskClient) -> None:
        """Verify an empty query is rejected before reaching Ollama."""
        response = client.post("/api/chat", json={"query": "   "})

        assert response.status_code == 400
        assert "error" in response.get_json()

    @patch("main.urllib.request.urlopen")
    def test_ollama_failure_returns_502(
        self, mock_urlopen: MagicMock, client: FlaskClient
    ) -> None:
        """Verify an upstream Ollama failure is reported as a 502, not a crash."""
        mock_urlopen.side_effect = OSError("connection refused")

        response = client.post("/api/chat", json={"query": "build battery firmware"})

        assert response.status_code == 502
        assert "error" in response.get_json()


class TestUploadRoute:
    """Tests for `POST /api/upload`."""

    def test_saves_file_to_assets_directory(
        self, client: FlaskClient, tmp_path: Path
    ) -> None:
        """Verify an uploaded file is written under the assets directory."""
        response = client.post(
            "/api/upload",
            data={"file": (BytesIO(b"weights-bytes"), "model.ckpt")},
            content_type="multipart/form-data",
        )

        assert response.status_code == 200
        filename = response.get_json()["filename"]
        saved_path = tmp_path / "assets" / filename
        assert saved_path.exists()
        assert saved_path.read_bytes() == b"weights-bytes"

    def test_sanitizes_path_traversal_filename(
        self, client: FlaskClient, tmp_path: Path
    ) -> None:
        """Verify a malicious filename cannot escape the assets directory."""
        response = client.post(
            "/api/upload",
            data={"file": (BytesIO(b"data"), "../../etc/passwd")},
            content_type="multipart/form-data",
        )

        assert response.status_code == 200
        filename = response.get_json()["filename"]
        saved_path = tmp_path / "assets" / filename
        assert saved_path.resolve().parent == (tmp_path / "assets").resolve()

    def test_rejects_missing_file(self, client: FlaskClient) -> None:
        """Verify a request with no file part is rejected."""
        response = client.post(
            "/api/upload", data={}, content_type="multipart/form-data"
        )

        assert response.status_code == 400
        assert "error" in response.get_json()

    def test_write_failure_returns_500_with_error_json(
        self, client: FlaskClient
    ) -> None:
        """Verify a filesystem error (e.g. permission denied) is reported as JSON.

        Mirrors a real misconfiguration: a Docker bind-mounted assets/ owned
        by a different user than the container process.
        """
        with patch(
            "werkzeug.datastructures.FileStorage.save",
            side_effect=PermissionError("denied"),
        ):
            response = client.post(
                "/api/upload",
                data={"file": (BytesIO(b"data"), "model.ckpt")},
                content_type="multipart/form-data",
            )

        assert response.status_code == 500
        assert "error" in response.get_json()
