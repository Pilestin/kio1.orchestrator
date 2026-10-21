"""Reading secrets from a `.env` file beside the project.

API keys belong in the environment, never in `config.json`. Asking someone
who is not a developer to set environment variables — differently in
PowerShell, cmd, and bash, and again after every restart — is asking them not
to use the provider. A `.env` file is the usual answer, and this reads one.

Only the standard library: the launcher calls this before any dependency is
installed.
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

DOTENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse `.env` text into a mapping.

    Accepts `NAME=value`, an optional `export ` prefix, blank lines, `#`
    comments, and values wrapped in single or double quotes. Anything else
    on a line is taken literally — a value is never expanded or evaluated.

    Args:
        text: The file's contents.

    Returns:
        Names to values, in file order.
    """
    values: dict[str, str] = {}

    for raw in text.splitlines():
        line = raw.strip()

        if not line or line.startswith("#"):
            continue

        if line.startswith("export "):
            line = line[len("export ") :].lstrip()

        name, separator, value = line.partition("=")
        name = name.strip()

        if not separator or not name.isidentifier():
            continue

        value = value.strip()

        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        elif " #" in value:
            # An unquoted trailing comment, as in `KEY=value  # note`.
            value = value.split(" #", 1)[0].rstrip()

        values[name] = value

    return values


def save_dotenv_value(name: str, value: str, path: Path = DOTENV_PATH) -> None:
    """Set one variable in a `.env` file, creating the file if needed.

    An existing line for the name is replaced in place; otherwise one is
    appended. Every other line — comments included — is kept as it was.

    Args:
        name: The variable, such as `OPENROUTER_API_KEY`.
        value: Its value. Written in double quotes, so spaces and `#`
            survive a read back.
        path: The file to write.

    Raises:
        ValueError: If the name is not a valid identifier, or the value spans
            lines or contains a double quote. Either would corrupt the file,
            and no API key contains one.
    """
    if not name.isidentifier():
        raise ValueError(f"{name!r} is not a valid variable name.")

    if any(character in value for character in ('"', "\n", "\r")):
        raise ValueError(
            "A value written to .env must be one line without double quotes."
        )

    line = f'{name}="{value}"'

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []

    for index, existing in enumerate(lines):
        stripped = existing.strip()

        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].lstrip()

        if stripped.partition("=")[0].strip() == name:
            lines[index] = line
            break
    else:
        lines.append(line)

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("Saved %s to %s", name, path.name)


def load_dotenv(path: Path = DOTENV_PATH) -> list[str]:
    """Copy a `.env` file's values into the environment.

    A variable already set wins: the environment is the more deliberate of
    the two, and a deployment that sets one must not be overridden by a file
    someone left in the checkout.

    Args:
        path: The file to read.

    Returns:
        The names that were set, for logging. Empty when there is no file.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError:
        logger.warning("Could not read %s", path, exc_info=True)
        return []

    applied = []

    for name, value in parse_dotenv(text).items():
        if name not in os.environ:
            os.environ[name] = value
            applied.append(name)

    if applied:
        # Names only: the values are secrets.
        logger.info("Loaded from %s: %s", path.name, ", ".join(applied))

    return applied
