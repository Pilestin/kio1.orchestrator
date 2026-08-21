import json
import logging
from pathlib import Path

from core.session_logger import generate_session_id

logger = logging.getLogger(__name__)


def create_chat_file(
    chat_directory: str, system_prompt: str, session_id: str | None = None
) -> Path:
    """Create a new chat history JSONL file with the system prompt as the first line.

    Args:
        chat_directory: Directory where chat files are stored.
        system_prompt: The system prompt to write as the first line.
        session_id: Shared timestamp+uuid id used to name the file, pairing it
            with the session's log file. A fresh id is generated when omitted.

    Returns:
        The `Path` to the newly created chat file.
    """
    directory = Path(chat_directory)
    directory.mkdir(parents=True, exist_ok=True)

    if session_id is None:
        session_id = generate_session_id()

    filepath = directory / f"chat_{session_id}.jsonl"
    line = json.dumps({"role": "system", "content": system_prompt})
    filepath.write_text(line + "\n", encoding="utf-8")
    logger.info("Created chat file: path=%s", filepath)
    return filepath


def append_user_message(chat_file: Path, message: str) -> None:
    """Append a user message to the chat history file.

    Args:
        chat_file: Path to the chat history file.
        message: The user's message to append.

    Returns:
        None.
    """
    line = json.dumps({"role": "user", "content": message})
    with chat_file.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def append_assistant_message(chat_file: Path, message: str) -> None:
    """Append an assistant message to the chat history file.

    Args:
        chat_file: Path to the chat history file.
        message: The assistant's response to append.

    Returns:
        None.
    """
    line = json.dumps({"role": "assistant", "content": message})
    with chat_file.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_messages(chat_file: Path) -> list[dict[str, str]]:
    """Load conversation messages from a chat file (excluding the system prompt).

    Args:
        chat_file: Path to the chat history file.

    Returns:
        A list of message dicts with `role` and `content` keys.
    """
    lines = chat_file.read_text(encoding="utf-8").strip().split("\n")
    messages: list[dict[str, str]] = []

    for line in lines[1:]:
        messages.append(json.loads(line))

    return messages
