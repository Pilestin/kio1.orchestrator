"""Tests for named arguments on an ordinary REST API.

An API such as a weather service wants a latitude and a longitude, not a
sentence. Declaring them puts them in the planner's catalog, the plan fills
them in, and the URL is built from them — or the step fails saying which one
was missing, rather than sending `{arguments.latitude}` to the far side.
"""

import io
import json
from typing import Any

import pytest

from core.prompt_loader import render_catalog
from dispatch.agent_registry import HttpToolSpec, load_agent_entry, load_agent_registry
from dispatch.agent_transport import AgentTransportError
from dispatch.tool_transport import HttpToolTransport
from dispatch.workflow_planner import ExecutionMessage

WEATHER = {
    "type": "http",
    "description": "Current weather.",
    "url": "https://weather.example/v1?lat={arguments.latitude}&lon={arguments.longitude}",
    "method": "GET",
    "arguments": {
        "latitude": {"type": "number", "required": True},
        "longitude": {"type": "number", "required": True},
        "units": {"type": "string"},
    },
    "capabilities": ["weather_check"],
}


def message(**data: Any) -> ExecutionMessage:
    """Return a step addressed to the weather API."""
    return ExecutionMessage(
        workflow_id="wf-1",
        step_id="s1",
        agent_id="Weather",
        capability="weather_check",
        endpoint=WEATHER["url"],
        task="Weather in Istanbul",
        data=data,
    )


@pytest.fixture
def requested(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Capture the URLs the transport calls instead of calling them."""
    import urllib.request

    urls: list[str] = []

    def fake_urlopen(request: Any, timeout: float) -> io.BytesIO:
        # A byte stream is all the transport reads from a response, and it
        # is already a context manager.
        urls.append(request.full_url)
        return io.BytesIO(json.dumps({"temperature": 21.5}).encode("utf-8"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    return urls


# ── reading the declaration ────────────────────────────────────────────────


def test_the_short_form_becomes_a_json_schema() -> None:
    spec = load_agent_entry("Weather", WEATHER).tool

    assert isinstance(spec, HttpToolSpec)
    assert spec.input_schema["required"] == ["latitude", "longitude"]
    assert spec.input_schema["properties"]["units"] == {"type": "string"}


def test_a_json_schema_is_accepted_as_it_is() -> None:
    schema = {
        "type": "object",
        "properties": {"q": {"type": "string", "description": "Search text"}},
        "required": ["q"],
    }
    spec = load_agent_entry(
        "Search", {"type": "http", "url": "https://s.example", "arguments": schema}
    ).tool

    assert isinstance(spec, HttpToolSpec)
    assert spec.input_schema == schema


def test_an_unknown_argument_type_is_refused() -> None:
    with pytest.raises(ValueError, match="type must be one of"):
        load_agent_entry(
            "Weather",
            {**WEATHER, "arguments": {"latitude": {"type": "float"}}},
        )


def test_the_planner_is_shown_what_the_api_takes() -> None:
    catalog = render_catalog(load_agent_registry({"Weather": WEATHER}))

    assert (
        "capabilities: `weather_check` · arguments: latitude (number, required), "
        "longitude (number, required), units (string)"
    ) in catalog


def test_an_api_without_arguments_is_listed_as_before() -> None:
    plain = {key: value for key, value in WEATHER.items() if key != "arguments"}
    catalog = render_catalog(load_agent_registry({"Weather": plain}))

    assert "arguments:" not in catalog


# ── calling it ─────────────────────────────────────────────────────────────


def test_the_url_is_built_from_the_plans_arguments(requested: list[str]) -> None:
    agent = load_agent_entry("Weather", WEATHER)

    reply = HttpToolTransport().send(
        message(arguments={"latitude": 41.01, "longitude": 28.97}), agent, 5
    )

    assert requested == ["https://weather.example/v1?lat=41.01&lon=28.97"]
    assert reply["status"] == "ok"
    assert reply["output"] == {"temperature": 21.5}


def test_a_missing_required_argument_fails_the_step_by_name(
    requested: list[str],
) -> None:
    agent = load_agent_entry("Weather", WEATHER)

    with pytest.raises(AgentTransportError, match="longitude"):
        HttpToolTransport().send(message(arguments={"latitude": 41.01}), agent, 5)

    assert requested == []


def test_no_arguments_at_all_fails_rather_than_guessing(requested: list[str]) -> None:
    agent = load_agent_entry("Weather", WEATHER)

    with pytest.raises(AgentTransportError, match="needs arguments"):
        HttpToolTransport().send(message(), agent, 5)

    assert requested == []


def test_a_single_text_argument_takes_the_task(requested: list[str]) -> None:
    search = {
        "type": "http",
        "url": "https://s.example/?q={arguments.q}",
        "method": "GET",
        "arguments": {"q": {"type": "string", "required": True}},
    }

    HttpToolTransport().send(message(), load_agent_entry("Search", search), 5)

    # Encoded: a raw space makes the request invalid, and a raw `&` in a task
    # would start a query parameter nobody configured.
    assert requested == ["https://s.example/?q=Weather%20in%20Istanbul"]
