"""Saving registry changes back into `config.json`.

The console can add, edit, and remove targets. Without this those changes
live in memory and are gone at the next restart, which makes the console a
viewer rather than a place you administer from.

Writing to `config.json` rather than to a file of its own keeps one mental
model: what the console shows is what the file says, and editing the file by
hand still works. It costs care in return, because that file also holds the
provider, telemetry, and policy settings:

- Only the `dispatch.agents` object is ever replaced. Every other key is
  read and written back exactly as it was found, in its original order.
- The write is atomic where the filesystem allows it: a temporary file in
  the same directory, then a replace. A single-file bind mount — how a
  container is given its configuration — cannot be replaced that way, so
  there the contents are rewritten in place instead.
- The previous contents are kept alongside as `config.json.bak`, so a bad
  save is one copy away from being undone.
- A deployment that ships a read-only config can turn writing off, and the
  console then says so instead of failing at the moment someone clicks Save.
"""

import errno
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Where `config.json` is, for the reader and the writer alike. A deployment
# that mounts its configuration elsewhere sets this once.
CONFIG_PATH_ENV = "KIO1_CONFIG_PATH"

# Set to a false-ish value where the configuration is mounted read-only, or
# where changes must only ever arrive through the repository.
CONFIG_WRITABLE_ENV = "KIO1_CONFIG_WRITABLE"

BACKUP_SUFFIX = ".bak"

# Why a rename can fail on a target that is perfectly writable. EBUSY is a
# single-file bind mount, which is how a container is usually given its
# configuration; EINVAL and EXDEV are the same story on other platforms and
# across filesystems. None of them means the file cannot be written, only
# that it cannot be replaced.
_RENAME_REFUSED = frozenset({errno.EBUSY, errno.EINVAL, errno.EXDEV, errno.EACCES})


class ConfigWriteError(Exception):
    """Raised when registry changes could not be saved."""


def config_path() -> Path:
    """Return the configuration file both the reader and writer use."""
    return Path(os.environ.get(CONFIG_PATH_ENV, "config.json"))


# What a first run starts from. The repository ships this rather than
# `config.json` itself, because the real file fills up with one person's
# workspace path, their agents, and their console edits, none of which
# belong in anyone else's checkout.
EXAMPLE_CONFIG = Path(__file__).resolve().parent.parent / "config.example.json"


def ensure_config(path: Path) -> bool:
    """Create the configuration file from the shipped example when it is missing.

    Args:
        path: Where the configuration is expected.

    Returns:
        True when a file was created, False when one was already there or
        there was no example to start from — in which case reading it fails
        with the ordinary missing-file error, naming the path.
    """
    if path.exists() or not EXAMPLE_CONFIG.is_file():
        return False

    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(EXAMPLE_CONFIG, path)
    logger.info("Created %s from %s", path, EXAMPLE_CONFIG.name)

    return True


def writing_enabled() -> bool:
    """Report whether saving to the configuration file is allowed.

    Returns:
        True unless the deployment turned it off. Defaults to on: a console
        you cannot save from is a surprise, and the failure mode of a
        read-only mount is caught below with a readable message anyway.
    """
    return os.environ.get(CONFIG_WRITABLE_ENV, "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


# Sections a settings update merges into rather than replaces.
_MERGED_SECTIONS = frozenset({"dispatch", "orchestration"})


def save_settings(updates: dict[str, Any]) -> Path:
    """Change named settings in the configuration file, leaving the rest alone.

    The console can change which model plans a run, and which code the agents
    are pointed at. Both were hand-edits into `config.json` until now, and both
    are the settings a demo gets wrong — so they are worth being able to change
    from the page that shows the result.

    Only the keys given are touched, and `dispatch` and `orchestration` are
    merged rather than replaced, so saving a workspace cannot drop the agents
    sitting beside it, nor saving the review model the round limit.

    Args:
        updates: Values to write. `dispatch` and `orchestration` entries are
            merged one level deep; every other key replaces its top-level
            value.

    Returns:
        The path that was written.

    Raises:
        ConfigWriteError: If writing is disabled, or the file could not be
            read or replaced.
    """
    if not writing_enabled():
        raise ConfigWriteError(
            "Saving to the configuration file is turned off "
            f"({CONFIG_WRITABLE_ENV} is set). Changes will only last until "
            "this process restarts."
        )

    path = config_path()
    document = _read_document()

    for key, value in updates.items():
        if key not in _MERGED_SECTIONS:
            document[key] = value
            continue

        section = document.setdefault(key, {})

        if not isinstance(section, dict):
            raise ConfigWriteError(
                f"{path} has a '{key}' that is not an object, so its "
                "settings cannot be changed."
            )

        if not isinstance(value, dict):
            raise ConfigWriteError(f"A '{key}' update must be an object.")

        section.update(value)

    _write_atomically(path, document)
    logger.info("Saved settings to %s: %s", path, sorted(updates))

    return path


def read_agents() -> dict[str, Any]:
    """Return the `dispatch.agents` object as the file currently holds it.

    Returns:
        The raw entries, keyed by agent id. Empty when the file names none.

    Raises:
        ConfigWriteError: If the file cannot be read or parsed.
    """
    return _read_document().get("dispatch", {}).get("agents", {})


def save_agents(agents: dict[str, Any]) -> Path:
    """Replace `dispatch.agents` in the configuration file.

    Args:
        agents: The complete set of entries to persist, keyed by agent id.

    Returns:
        The path that was written.

    Raises:
        ConfigWriteError: If writing is disabled, or the file could not be
            read or replaced.
    """
    if not writing_enabled():
        raise ConfigWriteError(
            "Saving to the configuration file is turned off "
            f"({CONFIG_WRITABLE_ENV} is set). Registry changes will only last "
            "until this process restarts."
        )

    path = config_path()
    document = _read_document()

    # `dispatch` may be absent in a minimal configuration, and a plain
    # assignment would then drop the rest of the block on the next read.
    dispatch = document.setdefault("dispatch", {})

    if not isinstance(dispatch, dict):
        raise ConfigWriteError(
            f"{path} has a 'dispatch' that is not an object, so its agents "
            "cannot be replaced."
        )

    dispatch["agents"] = agents

    _write_atomically(path, document)
    logger.info("Saved %d agents to %s", len(agents), path)

    return path


def _read_document() -> dict[str, Any]:
    """Return the whole configuration file, decoded.

    Returns:
        The decoded object.

    Raises:
        ConfigWriteError: If it cannot be read or is not a JSON object.
    """
    path = config_path()

    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ConfigWriteError(f"{path} could not be read: {error}") from error
    except json.JSONDecodeError as error:
        raise ConfigWriteError(f"{path} is not valid JSON: {error}") from error

    if not isinstance(decoded, dict):
        raise ConfigWriteError(f"{path} does not hold a JSON object.")

    return decoded


def _write_atomically(path: Path, document: dict[str, Any]) -> None:
    """Replace a file's contents without leaving it half-written.

    The preferred route is a temporary file in the same directory followed by
    a rename, which is atomic: a crash halfway through leaves the original
    untouched.

    That route is impossible when the target is a **single-file bind mount**,
    which is how `deploy/compose.yaml` gives the container its configuration.
    Such a file is a mount point, and the kernel refuses to rename over one —
    `EBUSY`. There the contents are rewritten in place instead. That is not
    atomic, so the backup taken just above is what a crash mid-write would be
    recovered from.

    Args:
        path: The file to replace.
        document: What to write.

    Raises:
        ConfigWriteError: If neither route could write the file.
    """
    rendered = json.dumps(document, indent=4, ensure_ascii=False) + "\n"
    directory = path.parent if str(path.parent) else Path()
    temporary: str | None = None

    try:
        if path.exists():
            shutil.copy2(path, path.with_suffix(path.suffix + BACKUP_SUFFIX))

        # Same directory as the target: a rename is only atomic within one
        # filesystem, and the system temporary directory is often another.
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=directory,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = handle.name
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())

        try:
            os.replace(temporary, path)
            temporary = None
        except OSError as error:
            if error.errno not in _RENAME_REFUSED:
                raise

            logger.info(
                "%s cannot be replaced by rename (%s); rewriting it in place. "
                "This is normal when the configuration is bind-mounted as a "
                "single file, as it is in a container.",
                path,
                error.strerror or error.errno,
            )
            _write_in_place(path, rendered)
    except OSError as error:
        backup = path.with_suffix(path.suffix + BACKUP_SUFFIX)
        recovery = (
            f" The previous contents are in {backup.name}."
            if backup.exists()
            else " The configuration is unchanged."
        )
        raise ConfigWriteError(
            f"{path} could not be written: {error}.{recovery}"
        ) from error
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def _write_in_place(path: Path, rendered: str) -> None:
    """Rewrite a file's contents through its existing inode.

    Used where a rename cannot reach the target. The file is opened for
    writing rather than replaced, so a bind mount, a symlink, or a hard link
    keeps pointing at the same contents.

    Args:
        path: The file to rewrite.
        rendered: What to write.

    Raises:
        OSError: If the file could not be opened or written.
    """
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(rendered)
        handle.flush()
        os.fsync(handle.fileno())
