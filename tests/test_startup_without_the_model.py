"""Tests for starting KIO1 while the model machine does not answer.

The property that matters: warming the model happens in the background, so a
model server that is off — or unreachable over Tailscale — never holds the
service, and the sign-in page with it, until a request times out minutes
later. Behind a public link, that wait was a 502.
"""

import json
import shutil
import threading
import time
from pathlib import Path
from typing import Any

import pytest

import app as service
import providers.ollama_client as ollama

ROOT = Path(__file__).resolve().parents[1]


def test_the_service_starts_while_the_model_machine_is_silent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    config["provider"] = "ollama"
    config["provider_options"]["endpoint"] = "http://100.71.36.18:11434"
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    shutil.copytree(ROOT / "prompts", tmp_path / "prompts")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KIO1_CONFIG_PATH", str(path))
    monkeypatch.setenv("KIO1_AGENT_STORE", "file")
    monkeypatch.setenv("KIO1_DATABASE_URL", f"sqlite:///{tmp_path / 'kio1.db'}")
    monkeypatch.delenv("KIO1_PROVIDER_ENDPOINT", raising=False)

    released = threading.Event()

    def silent(config: Any, client: Any) -> None:
        released.wait(30)  # a model machine that never answers

    monkeypatch.setattr(ollama, "preload", silent)

    began = time.monotonic()
    state = service._build_state()
    took = time.monotonic() - began
    released.set()
    state.directory.stop()

    assert took < 10
