"""Tests for agents that describe themselves.

The point is that adding an agent stops being a configuration edit. The agent
publishes what it can do, KIO1 asks, and the answer reaches both the routing
table and the planning prompt.
"""

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar

import pytest

from core.config_loader import DispatchConfig, load_config, resolve_discovery
from core.prompt_loader import render_catalog
from dispatch.agent_registry import (
    KIND_CLI,
    AgentEndpoint,
    AgentRegistry,
    load_agent_registry,
    merge_registries,
)
from dispatch.discovery import (
    DiscoveryConfig,
    DiscoveryError,
    ManifestSource,
    entries_from_manifest,
    fetch_entries,
    fetch_source,
    load_discovery_config,
)

SOURCE = ManifestSource(url="http://agents.test:8102/manifest")


class ManifestHandler(BaseHTTPRequestHandler):
    """Serves whatever manifest the test configured."""

    body: ClassVar[str] = ""
    status_code: ClassVar[int] = 200
    seen_headers: ClassVar[dict[str, str]] = {}

    def do_GET(self) -> None:
        type(self).seen_headers = dict(self.headers)
        payload = type(self).body.encode("utf-8")
        self.send_response(type(self).status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: Any) -> None:
        """Keep the test output clean."""


@pytest.fixture
def manifest_url() -> Iterator[str]:
    """Run a stub manifest server and yield its /manifest URL."""
    ManifestHandler.status_code = 200
    ManifestHandler.seen_headers = {}
    ManifestHandler.body = json.dumps(
        {
            "agent_id": "KIO7",
            "description": "Generates and explains code.",
            "capabilities": ["code_generation", "code_understanding"],
        }
    )

    server = ThreadingHTTPServer(("127.0.0.1", 0), ManifestHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    try:
        yield f"http://127.0.0.1:{int(server.server_address[1])}/manifest"
    finally:
        server.shutdown()
        server.server_close()


# ── manifest shapes ──────────────────────────────────────────────────────────


def test_a_single_agent_object_is_accepted() -> None:
    entries = entries_from_manifest(
        {"agent_id": "KIO7", "capabilities": ["code_generation"]}, SOURCE
    )

    assert entries["KIO7"]["capabilities"] == ["code_generation"]


def test_a_list_of_agents_is_accepted() -> None:
    entries = entries_from_manifest(
        [
            {"agent_id": "KIO7", "capabilities": ["code_generation"]},
            {"agent_id": "KIO11", "capabilities": ["test_automation"]},
        ],
        SOURCE,
    )

    assert sorted(entries) == ["KIO11", "KIO7"]


def test_the_config_json_shape_is_accepted() -> None:
    # A deployment can publish exactly the block it already maintains.
    entries = entries_from_manifest(
        {
            "agents": {
                "KIO7": {"capabilities": ["code_generation"]},
                "Pytest": {"type": "cli", "command": ["pytest"]},
            }
        },
        SOURCE,
    )

    assert sorted(entries) == ["KIO7", "Pytest"]
    assert entries["Pytest"]["type"] == "cli"


def test_an_agents_list_under_a_key_is_accepted() -> None:
    entries = entries_from_manifest(
        {"agents": [{"id": "KIO7", "capabilities": ["code_generation"]}]}, SOURCE
    )

    assert "KIO7" in entries


def test_an_agent_that_omits_its_address_gets_the_one_it_answered_on() -> None:
    # The one address a manifest cannot get wrong is the one KIO1 just used.
    entries = entries_from_manifest({"agent_id": "KIO7"}, SOURCE)

    assert entries["KIO7"]["endpoint"] == "http://agents.test:8102"


def test_an_agent_that_names_its_address_keeps_it() -> None:
    entries = entries_from_manifest(
        {"agent_id": "KIO7", "endpoint": "http://kio7.internal:9000"}, SOURCE
    )

    assert entries["KIO7"]["endpoint"] == "http://kio7.internal:9000"


def test_a_local_command_is_not_given_an_endpoint() -> None:
    entries = entries_from_manifest(
        {"agent_id": "Pytest", "type": "cli", "command": ["pytest"]}, SOURCE
    )

    assert "endpoint" not in entries["Pytest"]


def test_a_manifest_naming_no_agents_is_an_error() -> None:
    with pytest.raises(DiscoveryError, match="named no usable agents"):
        entries_from_manifest([], SOURCE)


def test_a_manifest_of_the_wrong_type_is_an_error() -> None:
    with pytest.raises(DiscoveryError, match="expected an agent object"):
        entries_from_manifest("KIO7", SOURCE)


def test_an_entry_without_an_agent_id_is_skipped() -> None:
    entries = entries_from_manifest(
        [{"capabilities": ["x"]}, {"agent_id": "KIO7"}], SOURCE
    )

    assert sorted(entries) == ["KIO7"]


# ── fetching ─────────────────────────────────────────────────────────────────


def test_a_live_agent_is_read_over_http(manifest_url: str) -> None:
    entries = fetch_source(ManifestSource(url=manifest_url), timeout=5)

    assert entries["KIO7"]["capabilities"] == ["code_generation", "code_understanding"]
    assert entries["KIO7"]["endpoint"].startswith("http://127.0.0.1:")


def test_an_unreachable_source_is_reported_not_raised_through() -> None:
    discovery = DiscoveryConfig(
        sources=(ManifestSource(url="http://127.0.0.1:1/manifest"),), timeout=1
    )

    assert fetch_entries(discovery) == {}


def test_an_unreachable_source_can_be_made_fatal() -> None:
    discovery = DiscoveryConfig(
        sources=(ManifestSource(url="http://127.0.0.1:1/manifest"),),
        timeout=1,
        required=True,
    )

    with pytest.raises(ValueError, match="required is true"):
        fetch_entries(discovery)


def test_one_dead_source_does_not_hide_a_live_one(manifest_url: str) -> None:
    discovery = DiscoveryConfig(
        sources=(
            ManifestSource(url="http://127.0.0.1:1/manifest"),
            ManifestSource(url=manifest_url),
        ),
        timeout=2,
    )

    assert "KIO7" in fetch_entries(discovery)


def test_a_non_json_answer_is_reported(manifest_url: str) -> None:
    ManifestHandler.body = "<html>not a manifest</html>"

    with pytest.raises(DiscoveryError, match="did not answer JSON"):
        fetch_source(ManifestSource(url=manifest_url), timeout=5)


def test_an_error_status_is_reported(manifest_url: str) -> None:
    ManifestHandler.status_code = 500

    with pytest.raises(DiscoveryError, match="HTTP 500"):
        fetch_source(ManifestSource(url=manifest_url), timeout=5)


def test_a_private_manifest_reads_its_token_from_the_environment(
    manifest_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KIO1_TEST_MANIFEST_TOKEN", "s3cret")

    fetch_source(
        ManifestSource(
            url=manifest_url,
            headers={"Authorization": "Bearer ${KIO1_TEST_MANIFEST_TOKEN}"},
        ),
        timeout=5,
    )

    assert ManifestHandler.seen_headers["Authorization"] == "Bearer s3cret"


def test_an_unset_token_is_reported_before_the_request(manifest_url: str) -> None:
    with pytest.raises(DiscoveryError, match="KIO1_TEST_UNSET_MANIFEST_TOKEN"):
        fetch_source(
            ManifestSource(
                url=manifest_url,
                headers={"Authorization": "Bearer ${KIO1_TEST_UNSET_MANIFEST_TOKEN}"},
            ),
            timeout=5,
        )


# ── merging with hand-written configuration ──────────────────────────────────


def endpoint(agent_id: str, host: str, capabilities: list[str]) -> AgentEndpoint:
    """Return a KIO entry for merge tests."""
    return AgentEndpoint(
        agent_id=agent_id,
        endpoint=host,
        path="/execute",
        capabilities=frozenset(capabilities),
    )


def test_configuration_wins_over_a_manifest() -> None:
    # A manifest cannot know the address KIO1 reaches it on from inside a
    # container. Whoever wrote the configuration does.
    configured = AgentRegistry(
        {
            "KIO7": endpoint(
                "KIO7", "http://host.docker.internal:8107", ["code_generation"]
            )
        }
    )
    discovered = AgentRegistry(
        {"KIO7": endpoint("KIO7", "http://127.0.0.1:8107", ["code_generation", "x"])}
    )
    merged = merge_registries(configured, discovered)
    kio7 = merged.resolve("KIO7")

    assert kio7 is not None
    assert kio7.endpoint == "http://host.docker.internal:8107"
    assert "x" not in kio7.capabilities


def test_a_manifest_adds_agents_configuration_never_mentioned() -> None:
    configured = AgentRegistry(
        {"KIO7": endpoint("KIO7", "http://a", ["code_generation"])}
    )
    discovered = AgentRegistry(
        {"KIO11": endpoint("KIO11", "http://b", ["test_automation"])}
    )

    assert merge_registries(configured, discovered).agent_ids() == {"KIO7", "KIO11"}


# ── the whole path, configuration to catalog ─────────────────────────────────


def test_discovery_reaches_both_the_registry_and_the_prompt(
    manifest_url: str,
) -> None:
    dispatch = DispatchConfig(
        enabled=True,
        registry=load_agent_registry(
            {
                "Pytest": {
                    "type": "cli",
                    "command": ["pytest"],
                    "capabilities": ["test_execution"],
                }
            }
        ),
        discovery=DiscoveryConfig(
            sources=(ManifestSource(url=manifest_url),), timeout=5
        ),
    )

    resolved = resolve_discovery(dispatch)
    catalog = render_catalog(resolved.registry)

    assert resolved.registry.agent_ids() == {"Pytest", "KIO7"}
    assert "**KIO7** — Generates and explains code." in catalog
    assert "`code_generation`" in catalog
    # The hand-written entry is untouched by discovery.
    local = resolved.registry.resolve("Pytest")
    assert local is not None and local.kind == KIND_CLI


def test_a_deployment_with_no_sources_makes_no_network_calls() -> None:
    dispatch = DispatchConfig(enabled=True, registry=AgentRegistry({}))

    assert resolve_discovery(dispatch) is dispatch


def test_a_broken_manifest_entry_is_dropped_not_fatal(manifest_url: str) -> None:
    # One agent publishing nonsense must not stop KIO1 from starting.
    ManifestHandler.body = json.dumps(
        [
            {"agent_id": "Broken", "type": "cli"},
            {"agent_id": "KIO7", "capabilities": ["code_generation"]},
        ]
    )
    dispatch = DispatchConfig(
        discovery=DiscoveryConfig(sources=(ManifestSource(url=manifest_url),))
    )

    assert resolve_discovery(dispatch).registry.agent_ids() == {"KIO7"}


def test_a_stub_agent_publishes_a_manifest_kio1_can_read() -> None:
    from tools.dummy_agent import build_server, server_address

    server = build_server(
        agent_id="KIO11",
        host="127.0.0.1",
        port=0,
        capabilities=frozenset({"test_automation", "validation"}),
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server_address(server)

    try:
        dispatch = resolve_discovery(
            DispatchConfig(
                discovery=DiscoveryConfig(
                    sources=(ManifestSource(url=f"http://{host}:{port}/manifest"),),
                    timeout=5,
                )
            )
        )
    finally:
        server.shutdown()
        server.server_close()

    kio11 = dispatch.registry.resolve("KIO11")

    assert kio11 is not None
    assert kio11.capabilities == frozenset({"test_automation", "validation"})
    assert kio11.url == f"http://{host}:{port}/execute"


# ── configuration ────────────────────────────────────────────────────────────


def test_an_absent_discovery_block_disables_it() -> None:
    assert load_discovery_config(None).sources == ()


def test_a_source_can_be_a_bare_url() -> None:
    discovery = load_discovery_config({"sources": ["http://127.0.0.1:8102/manifest"]})

    assert discovery.sources[0].url == "http://127.0.0.1:8102/manifest"
    assert discovery.sources[0].headers == {}


def test_a_source_can_carry_headers() -> None:
    discovery = load_discovery_config(
        {
            "sources": [
                {"url": "https://registry.test/agents", "headers": {"X-Key": "abc"}}
            ],
            "timeout": 2,
            "required": True,
        }
    )

    assert discovery.sources[0].headers == {"X-Key": "abc"}
    assert discovery.timeout == 2
    assert discovery.required is True


def test_a_source_that_is_not_a_url_is_rejected() -> None:
    with pytest.raises(ValueError, match="must be an HTTP"):
        load_discovery_config({"sources": ["agents.test/manifest"]})


def test_a_malformed_discovery_block_is_rejected() -> None:
    with pytest.raises(ValueError, match="sources must be a list"):
        load_discovery_config({"sources": "http://a/manifest"})


def test_the_repository_config_loads_with_discovery_absent() -> None:
    # Discovery is opt-in; the shipped configuration lists agents by hand.
    assert load_config("config.json").dispatch.discovery.sources == ()
