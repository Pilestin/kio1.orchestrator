"""Shared fixtures.

The console can save registry changes into `config.json`, which means the
test suite can too — and the first run of those tests rewrote the
repository's own configuration, deleting one agent and overwriting another.

The fix is not to be careful in each test. It is to make the repository's
file unreachable: every test session points `KIO1_CONFIG_PATH` at a copy in a
temporary directory, so anything that writes through `core.config_writer`
writes there. A test that means to read the shipped file still can, by naming
it explicitly — `load_config("config.json")` takes a path.
"""

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from core.config_writer import CONFIG_PATH_ENV

REPOSITORY_CONFIG = Path("config.json")


@pytest.fixture(scope="session", autouse=True)
def sandboxed_config(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Point the configured path at a throwaway copy for the whole session.

    Yields:
        The copy every write lands in.
    """
    sandbox = tmp_path_factory.mktemp("config") / "config.json"

    if REPOSITORY_CONFIG.exists():
        shutil.copy2(REPOSITORY_CONFIG, sandbox)
    else:  # pragma: no cover - the repository always ships one
        sandbox.write_text("{}", encoding="utf-8")

    previous = os.environ.get(CONFIG_PATH_ENV)
    os.environ[CONFIG_PATH_ENV] = str(sandbox)

    try:
        yield sandbox
    finally:
        if previous is None:
            os.environ.pop(CONFIG_PATH_ENV, None)
        else:
            os.environ[CONFIG_PATH_ENV] = previous


@pytest.fixture
def writable_config(sandboxed_config: Path, tmp_path: Path) -> Iterator[Path]:
    """Give one test a configuration file of its own to write to.

    The session-wide sandbox stops the repository's file being touched; this
    stops two tests in the same session treading on each other's saves.

    Yields:
        The file this test may write.
    """
    mine = tmp_path / "config.json"
    shutil.copy2(sandboxed_config, mine)
    os.environ[CONFIG_PATH_ENV] = str(mine)

    try:
        yield mine
    finally:
        os.environ[CONFIG_PATH_ENV] = str(sandboxed_config)
