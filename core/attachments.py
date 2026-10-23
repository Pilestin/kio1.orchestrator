"""Files attached to a request, kept where the agents can read them.

A person drops a file into the chat; it is stored under `data/attachments`,
one folder per file so two files of the same name never collide, and the run
is told where it is. A target that reads files reads it there. A target that
only reads text — an LLM agent — never sees a path, so a text file's content
also goes into the request the planner reads, and the planner copies what a
step needs into that step's task.
"""

import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_ROOT = Path("data") / "attachments"

# Large enough for source files, logs, and traces; small enough that one
# request cannot fill the disk or a request body.
MAX_BYTES = 2 * 1024 * 1024

# How much attached text the planner is given in all. A small local model
# loses the request itself in a long prompt; past this, a file is named but
# not quoted.
MAX_QUOTED_CHARS = 12_000

_ID = re.compile(r"^[0-9a-f]{32}$")
_UNSAFE = re.compile(r"[^\w.\- ]+")


class AttachmentError(ValueError):
    """A file could not be attached: too large, empty, or badly named."""


@dataclass(frozen=True)
class Attachment:
    """A stored file.

    Attributes:
        attachment_id: The folder it is stored in; what a request names.
        name: The file's name, made safe to store.
        size: Its length in bytes.
        path: Where it is, absolute, for targets that read files.
    """

    attachment_id: str
    name: str
    size: int
    path: Path

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON form a session and a step's `data` carry."""
        return {
            "attachment_id": self.attachment_id,
            "name": self.name,
            "size": self.size,
            "path": str(self.path),
        }

    def text(self) -> str | None:
        """Return the content as text, or None when it is not UTF-8 text."""
        try:
            content = self.path.read_bytes()
        except OSError:
            return None

        if b"\x00" in content[:4096]:
            return None  # binary, whatever its name says

        try:
            return content.decode("utf-8")
        except UnicodeDecodeError:
            return None


def safe_name(name: str) -> str:
    """Return a file name with no path in it and nothing a file system minds.

    Raises:
        AttachmentError: If nothing usable is left.
    """
    base = re.split(r"[\\/]", name.strip())[-1]
    cleaned = _UNSAFE.sub("_", base).strip(" .")[:120]

    if not cleaned:
        raise AttachmentError("The file has no usable name.")

    return cleaned


class AttachmentStore:
    """Files attached to requests, one folder each under a root."""

    def __init__(self, root: Path = DEFAULT_ROOT) -> None:
        self._root = root

    def save(self, name: str, content: bytes) -> Attachment:
        """Store a file and return it.

        Raises:
            AttachmentError: If it is empty, too large, or badly named.
        """
        if not content:
            raise AttachmentError("The file is empty.")

        if len(content) > MAX_BYTES:
            raise AttachmentError(
                f"The file is {len(content) // 1024} KB; the limit is "
                f"{MAX_BYTES // 1024} KB."
            )

        attachment_id = uuid.uuid4().hex
        folder = self._root / attachment_id
        folder.mkdir(parents=True, exist_ok=False)
        path = (folder / safe_name(name)).resolve()
        path.write_bytes(content)
        logger.info("Attachment stored: %s (%d bytes)", path, len(content))

        return Attachment(attachment_id, path.name, len(content), path)

    def get(self, attachment_id: str) -> Attachment | None:
        """Return a stored file, or None when there is no such file."""
        if not _ID.match(attachment_id):
            return None  # never a path someone typed

        folder = self._root / attachment_id
        files = [item for item in folder.glob("*") if item.is_file()]

        if len(files) != 1:
            return None

        path = files[0].resolve()

        return Attachment(attachment_id, path.name, path.stat().st_size, path)


def describe(attachments: list[Attachment]) -> str:
    """Write the attached files into a request, for the planner to read.

    Text is quoted until `MAX_QUOTED_CHARS` is spent, smallest files first so
    one large log does not crowd out the source file that matters; the rest
    are named with their path.

    Returns:
        The section, or an empty string when nothing is attached.
    """
    if not attachments:
        return ""

    budget = MAX_QUOTED_CHARS
    quoted: dict[str, str] = {}

    for attachment in sorted(attachments, key=lambda item: item.size):
        text = attachment.text()

        if text is not None and len(text) <= budget:
            quoted[attachment.attachment_id] = text
            budget -= len(text)

    sections = []

    for attachment in attachments:
        text = quoted.get(attachment.attachment_id)

        if text is None:
            sections.append(
                f"- {attachment.name} ({attachment.size} bytes, not quoted) "
                f"at {attachment.path}"
            )
        else:
            sections.append(
                f"- {attachment.name} at {attachment.path}:\n```\n{text.rstrip()}\n```"
            )

    return "Attached files:\n" + "\n".join(sections)
