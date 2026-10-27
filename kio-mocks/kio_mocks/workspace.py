"""The code under analysis, as KIO1 names it in `data.repository`.

KIO1 sends `data.repository.path` (and `data.target.entry_point`) with every
step; the path is the one *this* service can see. Everything a stand-in reads
or runs goes through here, so two rules hold everywhere:

- **Nothing outside the workspace is read.** A path in a task or an upstream
  output is resolved against the workspace and refused if it escapes it.
- **Nothing runs unbounded.** Every subprocess has a timeout, and its output
  is decoded as UTF-8 first, then the system's encoding — what KIO1 does too.
"""

from __future__ import annotations

import locale
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kio_mocks.errors import KioError

# Folders that are never the project's own code.
SKIPPED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "build",
        "dist",
        "site-packages",
        ".tox",
    }
)


def as_object(value: Any) -> dict[str, Any]:
    """`value` when it is a JSON object, else an empty one: messages are tolerated, not trusted."""
    return value if isinstance(value, dict) else {}


def decode(data: bytes | None) -> str:
    """Text a program printed, whichever encoding it printed in."""
    if not data:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode(locale.getpreferredencoding(False), errors="replace")


@dataclass(frozen=True)
class Completed:
    """What a command did."""

    command: list[str]
    returncode: int
    stdout: str
    stderr: str
    seconds: float
    cpu_seconds: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass(frozen=True)
class Workspace:
    """A project folder, or none when KIO1 was not told about one."""

    root: Path | None
    entry_point: str = ""

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> Workspace:
        repository = as_object(data.get("repository"))
        target = as_object(data.get("target"))
        path = str(repository.get("path") or "").strip()

        return cls(
            root=Path(path).expanduser().resolve() if path else None,
            entry_point=str(target.get("entry_point") or "").strip(),
        )

    def require(self) -> Path:
        """The folder, or a clear error saying what to set in KIO1."""
        if self.root is None:
            raise KioError(
                "No workspace was sent. Set the repository path in KIO1 "
                "(Settings → Advanced → Workspace) to the code to work on."
            )
        if not self.root.is_dir():
            raise KioError(
                f"The workspace {self.root} is not a folder this service can see."
            )
        return self.root

    def resolve(self, relative: str) -> Path:
        """A path inside the workspace; refused when it points outside."""
        root = self.require()
        candidate = (root / relative).resolve()

        if candidate != root and root not in candidate.parents:
            raise KioError(f"{relative!r} is outside the workspace.")

        return candidate

    def relative(self, path: Path) -> str:
        """`path` as the workspace-relative name people read."""
        root = self.require()
        try:
            return path.resolve().relative_to(root).as_posix()
        except ValueError:
            return path.as_posix()

    def python_files(self, limit: int = 2000) -> list[Path]:
        """The project's Python files, never a virtual environment's."""
        root = self.require()
        found: list[Path] = []

        for directory, folders, files in os.walk(root):
            folders[:] = sorted(
                name
                for name in folders
                if name not in SKIPPED_DIRS and not name.startswith(".")
            )
            for name in sorted(files):
                if name.endswith(".py"):
                    found.append(Path(directory) / name)
                    if len(found) >= limit:
                        return found

        return found

    def read(self, relative: str, max_bytes: int = 1_000_000) -> str:
        """A file's text, from inside the workspace."""
        path = self.resolve(relative)
        if not path.is_file():
            raise KioError(f"{relative!r} is not a file in the workspace.")
        return decode(path.read_bytes()[:max_bytes])

    def run(
        self,
        command: list[str],
        *,
        timeout: float = 40.0,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> Completed:
        """Run a command in the workspace, bounded, and say what it did."""
        root = cwd or self.require()
        started = time.perf_counter()
        before = _children_cpu()

        try:
            process = subprocess.Popen(
                command,
                cwd=root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                env={
                    **os.environ,
                    "PYTHONIOENCODING": "utf-8",
                    "PYTHONDONTWRITEBYTECODE": "1",
                    **(env or {}),
                },
            )
        except FileNotFoundError as error:
            raise KioError(
                f"{command[0]!r} is not installed where this service runs."
            ) from error

        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            process.kill()
            process.communicate()
            raise KioError(
                f"{' '.join(command)} did not finish within {timeout:g}s."
            ) from error

        # Per process where the system says (Windows); otherwise what all
        # finished children used meanwhile, which is this one when alone.
        cpu = _process_cpu(process)
        if cpu is None:
            cpu = max(0.0, _children_cpu() - before)

        return Completed(
            command=command,
            returncode=process.returncode,
            stdout=decode(stdout),
            stderr=decode(stderr),
            seconds=round(time.perf_counter() - started, 3),
            cpu_seconds=round(cpu, 3),
        )


def python() -> str:
    """The interpreter to run project code with: the one running this service."""
    return sys.executable


def _process_cpu(process: subprocess.Popen[bytes]) -> float | None:
    """A finished process's own CPU seconds, on Windows, which keeps them.

    `os.times()` reports no child CPU time on Windows, so without this every
    measurement there would fall back to wall time — startup included.
    """
    if os.name != "nt":
        return None

    import ctypes
    from ctypes import wintypes

    handle = getattr(process, "_handle", None)
    if handle is None:
        return None

    creation, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
    ok = ctypes.windll.kernel32.GetProcessTimes(  # type: ignore[attr-defined]
        wintypes.HANDLE(int(handle)),
        ctypes.byref(creation),
        ctypes.byref(exited),
        ctypes.byref(kernel),
        ctypes.byref(user),
    )
    if not ok:
        return None

    def seconds(value: wintypes.FILETIME) -> float:
        return ((value.dwHighDateTime << 32) + value.dwLowDateTime) / 10_000_000

    return seconds(kernel) + seconds(user)


def _children_cpu() -> float:
    """CPU seconds used by finished child processes, where the system tells."""
    try:
        times = os.times()
    except OSError:  # pragma: no cover - every supported system has os.times
        return 0.0
    return times.children_user + times.children_system
