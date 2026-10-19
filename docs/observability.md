# Observability

KIO1 uses OpenTelemetry to produce distributed traces and application metrics. Telemetry is exported over OTLP/HTTP to the shared AI4SWENG observability platform, which stores traces in Tempo, metrics in VictoriaMetrics, and visualizes both in Grafana.

Telemetry is disabled by default, so the orchestrator remains usable without any observability infrastructure or network access to the collector.

This repository does not run its own Tempo/Prometheus/Collector stack. KIO1 connects to the shared AI4SWENG observability platform the same way the other KIO modules do: it is configured with the platform's OTLP endpoint and sends telemetry directly to it over the network.

## Data Flow

```text
KIO1 Orchestrator (this machine)
        |
        | OTLP/HTTP  (telemetry.otlp_http_endpoint)
        v
AI4SWENG Observability Platform (remote)
        |
        +-- traces  --> Tempo
        +-- metrics --> VictoriaMetrics
        |
        v
     Grafana
```

The application sends telemetry directly to the platform's OpenTelemetry Collector over the network. The Collector, Tempo, VictoriaMetrics, and Grafana are owned and operated by the observability team, not by this repository.

## Services

| Service | Purpose | Configured via |
|---------|---------|-----------------|
| AI4SWENG Observability Collector | Receives and routes KIO1's telemetry | `telemetry.otlp_http_endpoint` in `config.json` |

The default endpoint currently points at the shared collector:

```text
http://157.230.17.89:4318
```

Before enabling telemetry, confirm the collector is reachable from this machine:

```bash
curl -v http://157.230.17.89:4318/v1/traces
```

A connection refusal or timeout means the collector is unreachable from this network (firewall, VPN, or the platform not currently running) — check with the observability owner before assuming an application bug. Any HTTP response, including an error status, means the network path is open.

## Collected Traces

The orchestrator produces the following spans:

| Span | Description |
|------|-------------|
| `kio1.startup` | Prompt, model, and session initialization |
| `kio1.provider.preload` | Ollama model preload |
| `kio1.turn` | Complete processing time for one user turn |
| `chat <model>` | Ollama request and response |
| `kio1.response.format` | Response parsing and workflow extraction |

The spans form parent-child relationships. For example, a `kio1.turn` span contains the model request and response-formatting spans.

Trace attributes include:

- Model
- Session ID
- Turn number
- Request duration
- Maximum output tokens
- Temperature
- Response model
- Response ID, when supplied by Ollama
- Finish reason
- Input and output token counts
- Workflow ID
- Workflow execution mode
- Workflow step count
- Error type

Every span and metric also carries the resource attributes `kio.id` and `deployment.environment` (see [Enabling Telemetry](#enabling-telemetry)), so the platform's dashboards can correlate KIO1's telemetry with the rest of the AI4SWENG fleet.

## Collected Metrics

| OpenTelemetry metric | Description |
|----------------------|-------------|
| `kio1.sessions.started` | Sessions that reached the interactive prompt |
| `kio1.sessions.completed` | Sessions that completed after entering the interactive loop |
| `kio1.turns` | User turns processed |
| `kio1.turn.duration` | End-to-end turn duration |
| `kio1.provider.preloads` | Provider preload or validation operations |
| `kio1.provider.preload.duration` | Provider preload or validation duration |
| `gen_ai.client.operation.duration` | Model request duration |
| `gen_ai.client.token.usage` | Input and output token usage |
| `kio1.conversation.messages` | Messages supplied to each model request |
| `kio1.response.truncations` | Responses stopped by an output or context limit |
| `kio1.response.format_fallbacks` | Responses parsed using the Python-literal fallback |
| `kio1.workflow.plans` | Workflow plans successfully parsed |
| `kio1.workflow.step.count` | Steps in parsed workflow plans |

In addition to the metrics above, KIO1 also emits the AI4SWENG Observability Integration Contract v1.0's mandatory metric set (contract section 2.1), so that KIO1 shows up correctly in the platform's shared dashboards (KIO dropdown, stale-KIO alert, etc.) alongside `kio2-sim`, `kio3`, `kio4`, and `kio7`:

| OpenTelemetry metric | Contract metric name | Description |
|-----------------------|-----------------------|-------------|
| `kio.request.count` | `kio_request_count` | Total requests processed, tagged `status` (one request = one user turn) |
| `kio.request.duration_ms` | `kio_request_duration_ms` | End-to-end request latency, in milliseconds |
| `kio.request.error_count` | `kio_request_error_count` | Errors, tagged `error_type` |
| `kio.llm.token_count` | `kio_llm_token_count` | Token usage, tagged `direction` (`input`/`output`) |
| `kio.llm.cost_usd` | `kio_llm_cost_usd` | Estimated cumulative LLM cost |
| `kio.session.active_count` | `kio_session_active_count` | Currently active sessions |
| `kio.heartbeat` | `kio_heartbeat` | Liveness signal, incremented every 60 seconds while telemetry is enabled |

Two of these are estimates, not measurements, and should be read accordingly on any dashboard:

- `kio_llm_cost_usd` is derived from a fixed per-provider USD/1K-token coefficient (`_COST_PER_1K_TOKENS_USD` in `telemetry.py`; currently `ollama=0.0`, `openai=0.002`, `anthropic=0.003`), not each provider's real invoice. It should be revisited before being used for real budget decisions.
- `kio_session_active_count` is best-effort: if the process is killed instead of exiting normally (`exit` in the KIO1 prompt or `Ctrl+C`), the corresponding decrement never runs and the count will not settle until telemetry re-initializes.

`kio.id` and `deployment.environment` are set as resource attributes (see [Enabling Telemetry](#enabling-telemetry)); the platform's Collector auto-promotes resource attributes to labels on ingest (`resource_to_telemetry_conversion: enabled: true` in `otel-collector/config.yaml`), so in principle that alone is enough. The seven contract metrics also attach `kio.id` explicitly on every call, matching the platform's own `kio-simulator` reference implementation (`kio2-sim`/`kio3`/`kio4`/`kio7`), which does the same rather than relying only on resource promotion — this removes any doubt about whether promotion applies identically across the Collector's gRPC (`4317`) and HTTP (`4318`) receiver paths.

The platform's Collector converts dots to underscores on ingest (`add_metric_suffixes: false`, so `kio1.turns` becomes `kio1_turns` rather than `kio1_turns_total`, and `kio.request.count` becomes `kio_request_count`). Histograms still produce `_bucket`, `_count`, and `_sum` series, since that is structurally required. Exact naming depends on the Collector's exporter configuration, which is owned by the observability platform, not this repository.

### D1.1 project-management KPIs (simulated)

D2.6 §4.1.1 (`FR-KIO1-08`) frames KIO1 as the integrator across all KIOs and requires "end-to-end observability across KIOs, LM calls, and infrastructure, including energy, hardware, and time utilisation." In that spirit, KIO1 also emits the full D1.1 KPI table (`AI4SWENG_KPI_Metrik_Referansi`), once per turn, using the exact metric names the platform's `kio-simulator` uses for the KIOs each KPI is assigned to — so these land in the same Grafana panels as `kio2-sim`/`kio3`/`kio4`/`kio7`/`kio8`/`kio13`.

**Every one of these is simulated**, not measured — random values inside the D1.1 baseline/target bands, always tagged `source=simulated`. There is no real measurement path for any of them yet (that would require, e.g., a real CI/CD integration for build/review timing, or real adoption tracking — out of scope here).

| OpenTelemetry metric | D1.1 KPI | Description |
|-----------------------|----------|--------------|
| `kio.codegen.duration_minutes` | 1.1 Code generation speed | |
| `kio.issue.resolution_hours` | 1.2 Issue resolution speed | |
| `kio.lifecycle_energy.pct_of_baseline` | 2.1 Lifecycle energy reduction | |
| `kio.deploy_energy.tokens_per_s_per_w` | 2.2 Deployment energy efficiency | |
| `kio.code_quality.score_pct` | 3.1 Code quality improvement | |
| `kio.review.score` | 3.2 Review score increase | |
| `kio.dev_productivity.features_per_day` | 4.1 Developer productivity | |
| `kio.time_to_market.days` | 5.1 Time-to-Market | |
| `kio.bugfix.duration_hours` | 6.1 Bug-fix time | |
| `kio.issue.customer_reported_count` | 6.2 Customer-reported issues | Rare event; only recorded on ~5% of errored turns |
| `kio.cost_saving.pct` | 7.1 Annual cost saving | |
| `kio.adoption.active_user_pct` | 8.1 Adoption rate | |
| `kio.adoption.usage_pct` | 8.2 Active usage & satisfaction (usage half) | |
| `kio.adoption.mos_score` | 8.2 Active usage & satisfaction (MOS half) | |
| `kio.cross_arch_build.success_count` | 8.3 Cross-Architecture Build Success Rate | Rare event; ~5% chance per turn regardless of outcome |
| `kio.refactoring.hours_per_feature` | 9.1 Refactoring reduction | |
| `kio.tech_debt.hours_per_100loc` | 9.2 Technical debt reduction | |

## Privacy

Telemetry does not contain:

- User prompts
- System-prompt contents
- Model-response contents
- Chat-history contents
- API keys
- Exception messages
- Exception stack traces

Traces contain selected operational identifiers such as session IDs, response IDs, and workflow IDs.

Metrics use low-cardinality labels such as provider, model, status, token type, execution mode, and error type. Per-session identifiers are not attached to metrics.

Application log files are separate from telemetry. Debug log files can contain request and response content, as documented in the troubleshooting guide.

## Requirements

The application requires Python and a supported model provider.

Enabling telemetry additionally requires network reachability to the shared observability platform's OTLP endpoint. No local Docker or Docker Compose installation is required — the platform is hosted remotely.

## Enabling Telemetry

### 1. Confirm collector reachability

```bash
curl -v http://157.230.17.89:4318/v1/traces
```

See [Services](#services) for how to interpret the result.

### 2. Set `telemetry.enabled` to `true` in `config.json`

```json
"telemetry": {
    "enabled": true,
    "service_name": "kio1-orchestrator",
    "otlp_http_endpoint": "http://157.230.17.89:4318",
    "metric_export_interval_ms": 5000,
    "trace_sample_ratio": 1.0,
    "kio_id": "kio1",
    "deployment_environment": "local"
}
```

`kio_id` identifies this service on the platform's shared dashboards (alongside `kio2-sim`, `kio3`, `kio4`, `kio7`, etc.). `deployment_environment` should be changed from `local` if this instance is not running on a developer machine (for example `staging`).

The configured `otlp_http_endpoint` is the OTLP/HTTP base URL. The application automatically appends:

```text
/v1/traces
/v1/metrics
```

### 3. Start KIO1

```bash
source .venv/bin/activate
python3 main.py
```

No other startup step is required — there is no local stack to bring up.

## Central Stack Operations

Storage retention, dashboards, alerting, and the lifecycle of the Collector/Tempo/VictoriaMetrics/Grafana stack are owned and operated by the AI4SWENG observability platform, not by this repository. This repository is only responsible for producing and exporting correct telemetry.

For Grafana access, dashboard URLs, or platform incidents, contact the observability platform owner rather than looking for local Docker Compose commands — none apply here anymore.

## Troubleshooting

For exporter failures, unavailable collector, and missing traces, see the [Troubleshooting Guide](troubleshooting.md#observability). Some subsections there still describe the previous local-Docker-Compose stack and are pending a follow-up revision to match the remote-platform setup described in this document.
