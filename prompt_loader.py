import logging
from pathlib import Path

from session_logger import measure_duration

logger = logging.getLogger(__name__)


def load_prompt(prompt_path: str) -> str:
    """Load a system prompt from a text file.

    Args:
        prompt_path: Path to the prompt file.

    Returns:
        The prompt text content as a string.
    """
    with measure_duration() as elapsed:
        content = Path(prompt_path).read_text().strip()

    logger.info(
        "Prompt loaded: path=%s duration_ms=%d chars=%d",
        prompt_path,
        elapsed(),
        len(content),
    )

    return content
