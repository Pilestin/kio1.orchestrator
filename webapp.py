"""Flask web UI: a messenger-style chat backed by the Ollama orchestrator.

Replaces the terminal REPL in `main.py` with a browser UI. The UI also
accepts model/dataset/file uploads, saved under `assets/<category>/`.
"""

import logging
import threading
import uuid
from formatter import format_json
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request
from werkzeug.utils import secure_filename

from config_loader import Config
from main import run_turn
from session_logger import measure_duration

logger = logging.getLogger("webapp")

ASSETS_DIRECTORY = Path("assets")

# Dataset/checkpoint uploads can be large; this bounds worst-case disk and
# memory usage from a single upload rather than leaving it unbounded.
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB

# Maps the UI's upload category to its assets/ subfolder. Looked up through
# this fixed mapping (never the raw request value) so a crafted "category"
# field cannot be used to traverse outside assets/.
UPLOAD_SUBDIRECTORIES = {
    "model": "models",
    "dataset": "datasets",
    "file": "files",
}
DEFAULT_UPLOAD_CATEGORY = "file"


class _ChatSession:
    """Mutable state for the single active chat conversation.

    The web UI serves one operator at a time, mirroring the single-session
    terminal REPL it replaces, so a shared session guarded by a lock is
    enough to keep turn numbering and chat-file writes consistent across
    concurrent requests.
    """

    def __init__(
        self,
        config: Config,
        system_prompt: str,
        chat_file: Path,
        session_id: str,
    ) -> None:
        self.config = config
        self.system_prompt = system_prompt
        self.chat_file = chat_file
        self.session_id = session_id
        self.turn = 0
        self.lock = threading.Lock()


def create_app(
    config: Config,
    system_prompt: str,
    chat_file: Path,
    session_id: str,
) -> Flask:
    """Build the Flask application serving the chat UI and upload endpoint.

    Args:
        config: Application configuration.
        system_prompt: The system prompt text.
        chat_file: Path to the session's chat history file.
        session_id: Shared session id used for tracing.

    Returns:
        A configured Flask application.
    """
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

    session = _ChatSession(config, system_prompt, chat_file, session_id)

    @app.get("/")
    def index() -> str:
        return render_template("index.html")

    @app.post("/api/chat")
    def chat() -> tuple[Response, int] | Response:
        payload = request.get_json(silent=True) or {}
        query = str(payload.get("query", "")).strip()

        if not query:
            return jsonify({"error": "Query must not be empty."}), 400

        with session.lock:
            session.turn += 1
            turn = session.turn
            logger.info("Turn started: turn=%d", turn)

            try:
                with measure_duration() as turn_elapsed:
                    content = run_turn(
                        config=session.config,
                        system_prompt=session.system_prompt,
                        chat_file=session.chat_file,
                        session_id=session.session_id,
                        turn=turn,
                        query=query,
                    )
            except Exception as error:
                logger.exception(
                    "Turn failed: turn=%d duration_ms=%d", turn, turn_elapsed()
                )
                return jsonify({"error": str(error)}), 502

        logger.info("Turn completed: turn=%d duration_ms=%d", turn, turn_elapsed())

        try:
            formatted = format_json(content)
        except ValueError as error:
            logger.warning("Response formatting failed: turn=%d error=%s", turn, error)
            return jsonify({"raw": content, "error": str(error)})

        return jsonify({"formatted": formatted})

    @app.post("/api/upload")
    def upload() -> tuple[Response, int] | Response:
        uploaded_file = request.files.get("file")

        if uploaded_file is None or not uploaded_file.filename:
            return jsonify({"error": "No file provided."}), 400

        safe_name = secure_filename(uploaded_file.filename)
        if not safe_name:
            return jsonify({"error": "Invalid file name."}), 400

        category = request.form.get("category", DEFAULT_UPLOAD_CATEGORY)
        subdirectory = UPLOAD_SUBDIRECTORIES.get(
            category, UPLOAD_SUBDIRECTORIES[DEFAULT_UPLOAD_CATEGORY]
        )

        # Prefixed with a short id so re-uploading a same-named file never
        # silently overwrites a previous one.
        stored_name = f"{uuid.uuid4().hex[:8]}_{safe_name}"
        relative_path = f"{subdirectory}/{stored_name}"
        destination = ASSETS_DIRECTORY / relative_path

        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            uploaded_file.save(destination)
        except OSError as error:
            logger.exception("File upload failed: name=%s", safe_name)
            return (
                jsonify(
                    {
                        "error": (
                            f"Could not save file on the server: {error}. "
                            "Check that assets/ is writable."
                        )
                    }
                ),
                500,
            )

        logger.info(
            "File uploaded: name=%s category=%s path=%s",
            safe_name,
            category,
            destination,
        )
        return jsonify({"filename": relative_path})

    return app
