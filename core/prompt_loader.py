from pathlib import Path


def load_prompt(prompt_path: str) -> str:
    """Load a system prompt from a text file.

    Args:
        prompt_path: Path to the prompt file.

    Returns:
        The prompt text content as a string.
    """
    path = Path(prompt_path)
    return path.read_text().strip()
