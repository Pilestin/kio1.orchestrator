"""Tests for saving registry changes back into `config.json`.

That file also holds the provider, telemetry, and policy settings, and a web
console rewrites it. Most of what follows is about the blast radius of a bad
save rather than about the save working.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from core.config_loader import load_config
from core.config_writer import (
    CONFIG_PATH_ENV,
    CONFIG_WRITABLE_ENV,
    ConfigWriteError,
    config_path,
    read_agents,
    save_agents,
    writing_enabled,
)

REPOSITORY_CONFIG = Path("config.json")

AGENT = {
    "endpoint": "http://127.0.0.1:9000",
    "description": "A target.",
    "capabilities": ["thing"],
}


def document(path: Path) -> dict[str, Any]:
    """Return a configuration file, decoded."""
    return json.loads(path.read_text(encoding="utf-8"))


def test_the_agents_block_is_replaced(writable_config: Path) -> None:
    save_agents({"Mine": AGENT})

    assert read_agents() == {"Mine": AGENT}


def test_nothing_outside_the_agents_block_is_touched(writable_config: Path) -> None:
    # The same file carries the provider, telemetry, and policy settings.
    before = document(writable_config)

    save_agents({"Mine": AGENT})

    after = document(writable_config)

    assert after["telemetry"] == before["telemetry"]
    assert after["provider"] == before["provider"]
    assert after["dispatch"]["policy"] == before["dispatch"]["policy"]
    assert after["dispatch"]["health"] == before["dispatch"]["health"]
    assert list(after) == list(before)


def test_the_saved_file_still_loads(writable_config: Path) -> None:
    save_agents({"Mine": AGENT})

    config = load_config(str(writable_config))

    assert config.dispatch.registry.agent_ids() == {"Mine"}
    assert config.dispatch.policy.gates_for("deployment")


def test_the_previous_contents_are_kept(writable_config: Path) -> None:
    before = writable_config.read_text(encoding="utf-8")

    save_agents({"Mine": AGENT})

    backup = writable_config.with_suffix(writable_config.suffix + ".bak")

    assert backup.read_text(encoding="utf-8") == before


def test_saving_can_be_turned_off(
    writable_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(CONFIG_WRITABLE_ENV, "0")
    before = writable_config.read_text(encoding="utf-8")

    with pytest.raises(ConfigWriteError, match=CONFIG_WRITABLE_ENV):
        save_agents({"Mine": AGENT})

    assert writable_config.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "FALSE"])
def test_the_ways_of_turning_it_off(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(CONFIG_WRITABLE_ENV, value)

    assert writing_enabled() is False


def test_it_is_on_unless_asked_otherwise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CONFIG_WRITABLE_ENV, raising=False)

    assert writing_enabled() is True


def test_a_file_that_is_not_json_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = tmp_path / "config.json"
    broken.write_text("{ not json", encoding="utf-8")
    monkeypatch.setenv(CONFIG_PATH_ENV, str(broken))

    with pytest.raises(ConfigWriteError, match="not valid JSON"):
        save_agents({"Mine": AGENT})


def test_a_missing_file_is_reported_not_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    absent = tmp_path / "nowhere" / "config.json"
    monkeypatch.setenv(CONFIG_PATH_ENV, str(absent))

    with pytest.raises(ConfigWriteError, match="could not be read"):
        save_agents({"Mine": AGENT})

    assert not absent.exists()


def test_a_dispatch_that_is_not_an_object_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    odd = tmp_path / "config.json"
    odd.write_text(json.dumps({"dispatch": "yes please"}), encoding="utf-8")
    monkeypatch.setenv(CONFIG_PATH_ENV, str(odd))

    with pytest.raises(ConfigWriteError, match="not an object"):
        save_agents({"Mine": AGENT})


def test_a_configuration_without_a_dispatch_block_gains_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    minimal = tmp_path / "config.json"
    minimal.write_text(json.dumps({"provider": "ollama"}), encoding="utf-8")
    monkeypatch.setenv(CONFIG_PATH_ENV, str(minimal))

    save_agents({"Mine": AGENT})

    assert document(minimal)["dispatch"]["agents"] == {"Mine": AGENT}
    assert document(minimal)["provider"] == "ollama"


def test_no_temporary_file_is_left_behind(writable_config: Path) -> None:
    save_agents({"Mine": AGENT})

    leftovers = [
        path.name for path in writable_config.parent.iterdir() if ".tmp" in path.name
    ]

    assert leftovers == []


def test_the_path_comes_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    elsewhere = tmp_path / "elsewhere.json"
    monkeypatch.setenv(CONFIG_PATH_ENV, str(elsewhere))

    assert config_path() == elsewhere


def test_the_test_suite_cannot_reach_the_repository_configuration() -> None:
    # The first run of these tests deleted an agent from the shipped
    # config.json, and the second run did it again through a test that
    # cleared the sandbox instead of restoring it. Hence `monkeypatch`
    # everywhere above, and hence this.
    assert config_path().resolve() != REPOSITORY_CONFIG.resolve()


# ── when the target cannot be replaced by a rename ──────────────────────────


def refuse_rename(errno_value: int) -> Any:
    """Return an os.replace that fails the way a bind mount does."""

    def replace(*args: Any, **kwargs: Any) -> None:
        raise OSError(errno_value, "Device or resource busy")

    return replace


def test_a_target_that_cannot_be_renamed_over_is_still_written(
    writable_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A single-file bind mount is a mount point, and the kernel refuses to
    # rename over one. This is what a container hits, and it is not a reason
    # to fail: the file itself is perfectly writable.
    import errno as errno_module
    import os

    monkeypatch.setattr(os, "replace", refuse_rename(errno_module.EBUSY))

    save_agents({"Mine": AGENT})

    assert read_agents() == {"Mine": AGENT}


def test_the_in_place_route_leaves_the_file_valid(
    writable_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import errno as errno_module
    import os

    monkeypatch.setattr(os, "replace", refuse_rename(errno_module.EBUSY))
    before = document(writable_config)

    save_agents({"Mine": AGENT})

    after = document(writable_config)

    assert after["telemetry"] == before["telemetry"]
    assert after["dispatch"]["policy"] == before["dispatch"]["policy"]
    assert load_config(str(writable_config)).dispatch.registry.agent_ids() == {"Mine"}


def test_no_temporary_file_survives_the_fallback(
    writable_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import errno as errno_module
    import os

    monkeypatch.setattr(os, "replace", refuse_rename(errno_module.EBUSY))

    save_agents({"Mine": AGENT})

    leftovers = [
        path.name for path in writable_config.parent.iterdir() if ".tmp" in path.name
    ]

    assert leftovers == []


def test_a_rename_failure_that_is_not_a_mount_still_fails(
    writable_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only the errors that mean "cannot be replaced" fall back. A genuinely
    # broken filesystem must still be reported.
    import errno as errno_module
    import os

    monkeypatch.setattr(os, "replace", refuse_rename(errno_module.ENOSPC))

    with pytest.raises(ConfigWriteError, match="could not be written"):
        save_agents({"Mine": AGENT})


def test_a_failed_write_says_where_the_previous_contents_are(
    writable_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import errno as errno_module
    import os

    monkeypatch.setattr(os, "replace", refuse_rename(errno_module.ENOSPC))

    with pytest.raises(ConfigWriteError, match=r"\.bak"):
        save_agents({"Mine": AGENT})
