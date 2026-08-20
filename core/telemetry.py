import logging
import random
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from time import perf_counter

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
    OTLPMetricExporter,
)
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.metrics import Counter, Histogram, Meter, UpDownCounter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import Span, SpanKind, Status, StatusCode, Tracer
from opentelemetry.util.types import Attributes, AttributeValue

from core.config_loader import TelemetryConfig

logger = logging.getLogger(__name__)

_INSTRUMENTATION_SCOPE = "kio1.orchestrator"

_GEN_AI_DURATION_BUCKETS = (
    0.01,
    0.02,
    0.04,
    0.08,
    0.16,
    0.32,
    0.64,
    1.28,
    2.56,
    5.12,
    10.24,
    20.48,
    40.96,
    81.92,
    163.84,
    327.68,
)

_TOKEN_USAGE_BUCKETS = (
    1,
    4,
    16,
    64,
    256,
    1024,
    4096,
    16384,
    65536,
    262144,
    1048576,
    4194304,
    16777216,
    67108864,
)

_ALLOWED_EXECUTION_MODES = {"sequential", "parallel", "mixed"}
_ALLOWED_TRUNCATION_REASONS = {
    "max_output_tokens",
    "context_window",
    "length",
}

# AI4SWENG Observability Integration Contract v1.0, section 2.1: mandatory
# metric set, expressed in milliseconds to match kio_request_duration_ms.
_REQUEST_DURATION_MS_BUCKETS = tuple(
    bucket * 1000 for bucket in _GEN_AI_DURATION_BUCKETS
)

_HEARTBEAT_INTERVAL_SECONDS = 60.0

# Rough, non-billing-accurate USD-per-1K-token estimate used only because the
# contract's kio_llm_cost_usd metric is mandatory and none of the providers
# KIO1 talks to expose real invoice data locally. Mirrors the simulated
# per-model coefficients used by the platform's kio3/kio4 KIO simulators.
_COST_PER_1K_TOKENS_USD: dict[str, float] = {
    "ollama": 0.0,
    "openai": 0.002,
    "anthropic": 0.003,
}


@dataclass(frozen=True)
class _Instruments:
    """OpenTelemetry metric instruments used by the orchestrator."""

    sessions_started: Counter
    sessions_completed: Counter
    turns: Counter
    turn_duration: Histogram
    provider_preloads: Counter
    provider_preload_duration: Histogram
    gen_ai_operation_duration: Histogram
    gen_ai_token_usage: Histogram
    conversation_messages: Histogram
    response_truncations: Counter
    format_fallbacks: Counter
    workflow_plans: Counter
    workflow_step_count: Histogram

    # Workflow dispatch layer (KIO1-KIO10 integration protocol): one
    # execution per plan that KIO1 actually routed to agents, and one
    # dispatch per execution message delivered to a single agent.
    workflow_executions: Counter
    workflow_execution_duration: Histogram
    step_dispatches: Counter
    step_duration: Histogram

    # AI4SWENG Observability Integration Contract v1.0, section 2.1:
    # mandatory metric set, one instrument per contract metric name.
    request_count: Counter
    request_duration_ms: Histogram
    request_error_count: Counter
    llm_token_count: Counter
    llm_cost_usd: Counter
    session_active_count: UpDownCounter
    heartbeat: Counter

    # D1.1 project-management KPIs (AI4SWENG_KPI_Metrik_Referansi), simulated.
    # KIO1 is D2.6's "AI4SWEng AI Engineering Suite" — the integrator across
    # all KIOs — so unlike the platform's kio-simulator (which only emits the
    # KPI subset matching its own KIO_REAL_KPI_ROLE), KIO1 emits the full
    # table. Names/units match kio-simulator/kio_simulator.py exactly so they
    # land in the same Grafana panels as kio2-sim/kio3/kio4/kio7/kio8/kio13.
    kpi_codegen_duration: Histogram  # 1.1
    kpi_issue_resolution: Histogram  # 1.2
    kpi_lifecycle_energy: Histogram  # 2.1
    kpi_deploy_energy_efficiency: Histogram  # 2.2
    kpi_code_quality: Histogram  # 3.1
    kpi_review_score: Histogram  # 3.2
    kpi_dev_productivity: Histogram  # 4.1
    kpi_time_to_market: Histogram  # 5.1
    kpi_bugfix_duration: Histogram  # 6.1
    kpi_customer_reported_issues: Counter  # 6.2
    kpi_cost_saving: Histogram  # 7.1
    kpi_adoption_rate: Histogram  # 8.1
    kpi_adoption_usage: Histogram  # 8.2 (usage half)
    kpi_adoption_mos: Histogram  # 8.2 (MOS half)
    kpi_cross_arch_build: Counter  # 8.3
    kpi_refactoring: Histogram  # 9.1
    kpi_tech_debt: Histogram  # 9.2


_tracer_provider: TracerProvider | None = None
_meter_provider: MeterProvider | None = None
_tracer: Tracer = trace.get_tracer(_INSTRUMENTATION_SCOPE)
_meter: Meter = metrics.get_meter(_INSTRUMENTATION_SCOPE)
_instruments: _Instruments | None = None
_heartbeat_thread: threading.Thread | None = None
_heartbeat_stop_event: threading.Event | None = None
# Explicit per-metric kio.id, in addition to the kio.id resource attribute.
# The platform's own kio-simulator reference implementation attaches kio.id
# on every metric call rather than relying solely on the Collector's
# resource_to_telemetry_conversion — mirrored here so KIO1's contract
# metrics are labeled the same way as kio2-sim/kio3/kio4/kio7.
_kio_id: str = ""


def _create_instruments(meter: Meter) -> _Instruments:
    """Create all application metric instruments."""

    return _Instruments(
        sessions_started=meter.create_counter(
            "kio1.sessions.started",
            unit="{session}",
            description="Number of orchestrator sessions started.",
        ),
        sessions_completed=meter.create_counter(
            "kio1.sessions.completed",
            unit="{session}",
            description="Number of orchestrator sessions completed.",
        ),
        turns=meter.create_counter(
            "kio1.turns",
            unit="{turn}",
            description="Number of user turns processed.",
        ),
        turn_duration=meter.create_histogram(
            "kio1.turn.duration",
            unit="s",
            description="End-to-end duration of a user turn.",
            explicit_bucket_boundaries_advisory=_GEN_AI_DURATION_BUCKETS,
        ),
        provider_preloads=meter.create_counter(
            "kio1.provider.preloads",
            unit="{preload}",
            description="Number of provider preload operations.",
        ),
        provider_preload_duration=meter.create_histogram(
            "kio1.provider.preload.duration",
            unit="s",
            description="Duration of provider preload operations.",
            explicit_bucket_boundaries_advisory=_GEN_AI_DURATION_BUCKETS,
        ),
        gen_ai_operation_duration=meter.create_histogram(
            "gen_ai.client.operation.duration",
            unit="s",
            description="Duration of generative AI client operations.",
            explicit_bucket_boundaries_advisory=_GEN_AI_DURATION_BUCKETS,
        ),
        gen_ai_token_usage=meter.create_histogram(
            "gen_ai.client.token.usage",
            unit="{token}",
            description="Number of input and output tokens used.",
            explicit_bucket_boundaries_advisory=_TOKEN_USAGE_BUCKETS,
        ),
        conversation_messages=meter.create_histogram(
            "kio1.conversation.messages",
            unit="{message}",
            description="Number of messages supplied to a model request.",
        ),
        response_truncations=meter.create_counter(
            "kio1.response.truncations",
            unit="{response}",
            description="Number of model responses truncated before completion.",
        ),
        format_fallbacks=meter.create_counter(
            "kio1.response.format_fallbacks",
            unit="{response}",
            description="Number of responses requiring the Python-literal fallback.",
        ),
        workflow_plans=meter.create_counter(
            "kio1.workflow.plans",
            unit="{plan}",
            description="Number of workflow plans successfully parsed.",
        ),
        workflow_step_count=meter.create_histogram(
            "kio1.workflow.step.count",
            unit="{step}",
            description="Number of steps in generated workflow plans.",
        ),
        workflow_executions=meter.create_counter(
            "kio1.workflow.executions",
            unit="{execution}",
            description="Number of workflow plans dispatched to KIO agents.",
        ),
        workflow_execution_duration=meter.create_histogram(
            "kio1.workflow.execution.duration",
            unit="s",
            description="End-to-end duration of dispatching a whole workflow.",
            explicit_bucket_boundaries_advisory=_GEN_AI_DURATION_BUCKETS,
        ),
        step_dispatches=meter.create_counter(
            "kio1.step.dispatches",
            unit="{step}",
            description="Number of execution messages delivered to KIO agents.",
        ),
        step_duration=meter.create_histogram(
            "kio1.step.duration",
            unit="s",
            description="Duration of a single agent dispatch, reply included.",
            explicit_bucket_boundaries_advisory=_GEN_AI_DURATION_BUCKETS,
        ),
        request_count=meter.create_counter(
            "kio.request.count",
            unit="1",
            description=(
                "Total number of requests processed (contract metric "
                "kio_request_count)."
            ),
        ),
        request_duration_ms=meter.create_histogram(
            "kio.request.duration_ms",
            unit="ms",
            description=(
                "End-to-end request latency distribution (contract metric "
                "kio_request_duration_ms)."
            ),
            explicit_bucket_boundaries_advisory=_REQUEST_DURATION_MS_BUCKETS,
        ),
        request_error_count=meter.create_counter(
            "kio.request.error_count",
            unit="1",
            description=(
                "Errors categorized by a bounded error type (contract "
                "metric kio_request_error_count)."
            ),
        ),
        llm_token_count=meter.create_counter(
            "kio.llm.token_count",
            unit="{token}",
            description=(
                "LLM token usage by direction (contract metric " "kio_llm_token_count)."
            ),
        ),
        llm_cost_usd=meter.create_counter(
            "kio.llm.cost_usd",
            unit="USD",
            description=(
                "Estimated cumulative LLM cost (contract metric "
                "kio_llm_cost_usd). Derived from a fixed per-provider "
                "USD/1K-token coefficient, not real billing data."
            ),
        ),
        session_active_count=meter.create_up_down_counter(
            "kio.session.active_count",
            unit="1",
            description=(
                "Number of currently active sessions (contract metric "
                "kio_session_active_count)."
            ),
        ),
        heartbeat=meter.create_counter(
            "kio.heartbeat",
            unit="1",
            description=(
                "Background liveness signal incremented every "
                f"{int(_HEARTBEAT_INTERVAL_SECONDS)} seconds while "
                "telemetry is enabled (contract metric kio_heartbeat)."
            ),
        ),
        kpi_codegen_duration=meter.create_histogram(
            "kio.codegen.duration_minutes",
            unit="min",
            description="D1.1 KPI 1.1 Code generation speed (simulated).",
        ),
        kpi_issue_resolution=meter.create_histogram(
            "kio.issue.resolution_hours",
            unit="h",
            description="D1.1 KPI 1.2 Issue resolution speed (simulated).",
        ),
        kpi_lifecycle_energy=meter.create_histogram(
            "kio.lifecycle_energy.pct_of_baseline",
            unit="%",
            description="D1.1 KPI 2.1 Lifecycle energy reduction (simulated).",
        ),
        kpi_deploy_energy_efficiency=meter.create_histogram(
            "kio.deploy_energy.tokens_per_s_per_w",
            unit="1",
            description="D1.1 KPI 2.2 Deployment energy efficiency (simulated).",
        ),
        kpi_code_quality=meter.create_histogram(
            "kio.code_quality.score_pct",
            unit="%",
            description="D1.1 KPI 3.1 Code quality improvement (simulated).",
        ),
        kpi_review_score=meter.create_histogram(
            "kio.review.score",
            unit="1",
            description="D1.1 KPI 3.2 Review score increase (simulated).",
        ),
        kpi_dev_productivity=meter.create_histogram(
            "kio.dev_productivity.features_per_day",
            unit="1",
            description="D1.1 KPI 4.1 Developer productivity (simulated).",
        ),
        kpi_time_to_market=meter.create_histogram(
            "kio.time_to_market.days",
            unit="d",
            description="D1.1 KPI 5.1 Time-to-Market (simulated).",
        ),
        kpi_bugfix_duration=meter.create_histogram(
            "kio.bugfix.duration_hours",
            unit="h",
            description="D1.1 KPI 6.1 Bug-fix time (simulated).",
        ),
        kpi_customer_reported_issues=meter.create_counter(
            "kio.issue.customer_reported_count",
            unit="1",
            description="D1.1 KPI 6.2 Customer-reported issues (simulated).",
        ),
        kpi_cost_saving=meter.create_histogram(
            "kio.cost_saving.pct",
            unit="%",
            description="D1.1 KPI 7.1 Annual cost saving (simulated).",
        ),
        kpi_adoption_rate=meter.create_histogram(
            "kio.adoption.active_user_pct",
            unit="%",
            description="D1.1 KPI 8.1 Adoption rate (simulated).",
        ),
        kpi_adoption_usage=meter.create_histogram(
            "kio.adoption.usage_pct",
            unit="%",
            description="D1.1 KPI 8.2 Active usage, usage half (simulated).",
        ),
        kpi_adoption_mos=meter.create_histogram(
            "kio.adoption.mos_score",
            unit="1",
            description="D1.1 KPI 8.2 Active usage, satisfaction/MOS half (simulated).",
        ),
        kpi_cross_arch_build=meter.create_counter(
            "kio.cross_arch_build.success_count",
            unit="1",
            description=(
                "D1.1 KPI 8.3 Cross-Architecture Build Success Rate (simulated)."
            ),
        ),
        kpi_refactoring=meter.create_histogram(
            "kio.refactoring.hours_per_feature",
            unit="h",
            description="D1.1 KPI 9.1 Refactoring reduction (simulated).",
        ),
        kpi_tech_debt=meter.create_histogram(
            "kio.tech_debt.hours_per_100loc",
            unit="h",
            description="D1.1 KPI 9.2 Technical debt reduction (simulated).",
        ),
    )


def init_telemetry(
    config: TelemetryConfig,
    *,
    llm: str,
    task_type: str = "orchestration",
) -> None:
    """Initialize OTLP trace and metric exporters.

    Args:
        config: Validated telemetry configuration.
        llm: The configured model name (config.model), attached as the
            optional bounded-enum `llm` resource attribute — matching the
            platform's kio-simulator convention, so the KIO Detail
            dashboard's "LLM" panel resolves for KIO1. KIO1 loads its
            provider/model once at startup and never changes it mid-process
            (see config_loader.load_config), so this is safe as a static
            resource attribute, exactly like the simulators.
        task_type: The optional bounded-enum `task_type` resource attribute.
            KIO1 doesn't have a per-request task type the way the KIO
            simulators do (code-analysis, nlp-requirements, ...); it always
            does the same job, so this defaults to a fixed literal
            describing that job.

    Returns:
        None.
    """
    global _instruments
    global _kio_id
    global _meter
    global _meter_provider
    global _tracer
    global _tracer_provider

    if not config.enabled:
        logger.info("OpenTelemetry export is disabled")
        return

    if _tracer_provider is not None or _meter_provider is not None:
        logger.debug("OpenTelemetry is already initialized")
        return

    resource = Resource.create(
        {
            "service.name": config.service_name,
            "kio.id": config.kio_id,
            "deployment.environment": config.deployment_environment,
            # Optional bounded-enum labels (G3-compliant), matching the
            # platform's kio-simulator convention (Contract §1.2).
            "llm": llm,
            "task_type": task_type,
        }
    )

    headers = (
        {"Authorization": f"Bearer {config.otlp_bearer_token}"}
        if config.otlp_bearer_token
        else None
    )
    if headers is None:
        logger.warning(
            "core.telemetry.otlp_bearer_token is not set; OTLP requests will be "
            "sent without an Authorization header"
        )

    trace_exporter = OTLPSpanExporter(
        endpoint=f"{config.otlp_http_endpoint}/v1/traces",
        headers=headers,
    )
    metric_exporter = OTLPMetricExporter(
        endpoint=f"{config.otlp_http_endpoint}/v1/metrics",
        headers=headers,
    )

    tracer_provider = TracerProvider(
        resource=resource,
        sampler=ParentBased(
            TraceIdRatioBased(config.trace_sample_ratio),
        ),
        shutdown_on_exit=False,
    )
    tracer_provider.add_span_processor(
        BatchSpanProcessor(trace_exporter),
    )

    metric_reader = PeriodicExportingMetricReader(
        metric_exporter,
        export_interval_millis=config.metric_export_interval_ms,
    )
    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[metric_reader],
        shutdown_on_exit=False,
    )

    trace.set_tracer_provider(tracer_provider)
    metrics.set_meter_provider(meter_provider)

    _tracer_provider = tracer_provider
    _meter_provider = meter_provider
    _tracer = tracer_provider.get_tracer(_INSTRUMENTATION_SCOPE)
    _meter = meter_provider.get_meter(_INSTRUMENTATION_SCOPE)
    _instruments = _create_instruments(_meter)
    _kio_id = config.kio_id

    _start_heartbeat(_instruments, _kio_id)

    logger.info(
        "OpenTelemetry initialized: service=%s endpoint=%s",
        config.service_name,
        config.otlp_http_endpoint,
    )


def _heartbeat_loop(
    instruments: _Instruments,
    stop_event: threading.Event,
    kio_id: str,
) -> None:
    """Increment the heartbeat counter every interval until stopped.

    Args:
        instruments: The metric instruments to record onto.
        stop_event: Signaled to stop the loop and exit the thread promptly.
        kio_id: Attached to every heartbeat data point, matching the
            platform's kio-simulator convention of labeling kio.heartbeat
            explicitly rather than relying only on resource promotion.

    Returns:
        None.
    """
    while not stop_event.wait(_HEARTBEAT_INTERVAL_SECONDS):
        try:
            instruments.heartbeat.add(1, {"kio.id": kio_id})
        except Exception:
            logger.exception("Failed to record the kio.heartbeat metric")


def _start_heartbeat(instruments: _Instruments, kio_id: str) -> None:
    """Start the background heartbeat thread, replacing any existing one."""

    global _heartbeat_thread
    global _heartbeat_stop_event

    _stop_heartbeat()

    stop_event = threading.Event()
    thread = threading.Thread(
        target=_heartbeat_loop,
        args=(instruments, stop_event, kio_id),
        name="kio1-otel-heartbeat",
        daemon=True,
    )
    _heartbeat_stop_event = stop_event
    _heartbeat_thread = thread
    thread.start()


def _stop_heartbeat() -> None:
    """Signal the heartbeat thread to stop and wait briefly for it to exit."""

    global _heartbeat_thread
    global _heartbeat_stop_event

    if _heartbeat_stop_event is not None:
        _heartbeat_stop_event.set()

    if _heartbeat_thread is not None:
        _heartbeat_thread.join(timeout=1.0)

    _heartbeat_thread = None
    _heartbeat_stop_event = None


def shutdown_telemetry() -> None:
    """Flush pending telemetry and shut down exporters safely."""

    _stop_heartbeat()

    meter_provider = _meter_provider
    tracer_provider = _tracer_provider

    if meter_provider is not None:
        try:
            meter_provider.shutdown()
        except Exception:
            logger.exception("Failed to shut down the OpenTelemetry meter provider")

    if tracer_provider is not None:
        try:
            tracer_provider.shutdown()
        except Exception:
            logger.exception("Failed to shut down the OpenTelemetry tracer provider")


def _record_kpi_snapshot(is_error: bool) -> None:
    """Record one simulated sample of every D1.1 project-management KPI.

    Values are randomized within the baseline/target bands from
    AI4SWENG_KPI_Metrik_Referansi (same ranges used by the platform's
    kio-simulator for the KIOs it assigns each KPI to), not derived from
    anything KIO1 actually measured — every data point carries
    source="simulated" so it's distinguishable on the platform's dashboards
    once a real measurement path exists.

    Args:
        is_error: Whether the current turn ended in an error. Only affects
            the rare, event-style KPIs (6.2, 8.3), matching kio-simulator's
            own gating.

    Returns:
        None.
    """
    instruments = _instruments
    if instruments is None:
        return

    labels = {"kio.id": _kio_id, "source": "simulated"}

    # KPI 1.1 — Code generation speed: baseline ~100-120 min, target <=70%.
    instruments.kpi_codegen_duration.record(
        round(random.uniform(65.0, 95.0), 1), labels
    )
    # KPI 1.2 — Issue resolution speed: baseline ~8-12h, target <=70%.
    instruments.kpi_issue_resolution.record(round(random.uniform(5.0, 9.0), 2), labels)
    # KPI 2.1 — Lifecycle energy reduction: baseline 100%, target <=85%.
    instruments.kpi_lifecycle_energy.record(
        round(random.uniform(78.0, 96.0), 1), labels
    )
    # KPI 2.2 — Deployment energy efficiency (tokens/s/W): target >=15% over
    # an assumed ~7.5 tok/s/W unoptimized baseline.
    instruments.kpi_deploy_energy_efficiency.record(
        round(random.uniform(6.5, 10.5), 2), labels
    )
    # KPI 3.1 — Code quality improvement: baseline 100%, target <=70%.
    instruments.kpi_code_quality.record(round(random.uniform(65.0, 90.0), 1), labels)
    # KPI 3.2 — Review score increase: baseline ~3.5/5, target ~4.2/5.
    instruments.kpi_review_score.record(round(random.uniform(3.6, 4.4), 2), labels)
    # KPI 4.1 — Developer productivity: baseline ~0.5-0.8 features/day.
    instruments.kpi_dev_productivity.record(round(random.uniform(0.6, 1.1), 2), labels)
    # KPI 5.1 — Time-to-Market: baseline ~5-7 days scaled to D1.1's ~30-45
    # day pilot-feature baseline, target <=70%.
    instruments.kpi_time_to_market.record(round(random.uniform(24.0, 38.0), 1), labels)
    # KPI 6.1 — Bug-fix time: baseline ~8-12h, target <=80%.
    instruments.kpi_bugfix_duration.record(round(random.uniform(6.0, 10.0), 2), labels)
    # KPI 6.2 — Customer-reported issues: rare event, only on some errors.
    if is_error and random.random() < 0.05:
        instruments.kpi_customer_reported_issues.add(random.randint(1, 2), labels)
    # KPI 7.1 — Annual cost saving: target range ~12-28%.
    instruments.kpi_cost_saving.record(round(random.uniform(12.0, 28.0), 1), labels)
    # KPI 8.1 — Adoption rate: baseline 0%, target >=50%, simulated mid-ramp.
    instruments.kpi_adoption_rate.record(round(random.uniform(32.0, 58.0), 1), labels)
    # KPI 8.2 — Active usage & satisfaction: usage target >=60%, MOS >=4.0.
    instruments.kpi_adoption_usage.record(round(random.uniform(45.0, 68.0), 1), labels)
    instruments.kpi_adoption_mos.record(round(random.uniform(3.4, 4.3), 2), labels)
    # KPI 8.3 — Cross-Architecture Build Success Rate: rare, discrete event.
    if random.random() < 0.05:
        instruments.kpi_cross_arch_build.add(1, labels)
    # KPI 9.1 — Refactoring effort reduction: baseline ~2-3h/feature.
    instruments.kpi_refactoring.record(round(random.uniform(2.5, 4.5), 2), labels)
    # KPI 9.2 — Technical debt reduction: baseline ~1.0-1.2h/100loc.
    instruments.kpi_tech_debt.record(round(random.uniform(1.8, 3.5), 2), labels)


def _model_attributes(provider: str, model: str) -> dict[str, AttributeValue]:
    """Return low-cardinality provider and model attributes."""

    return {
        "gen_ai.provider.name": provider,
        "gen_ai.request.model": model,
    }


def _mark_span_error(span: Span, error: BaseException) -> None:
    """Mark a span as failed without exporting the exception message."""

    span.set_attribute("error.type", type(error).__name__)
    span.set_status(Status(StatusCode.ERROR))


@contextmanager
def trace_operation(
    name: str,
    attributes: Attributes = None,
    *,
    kind: SpanKind = SpanKind.INTERNAL,
) -> Iterator[Span]:
    """Create a span and attach privacy-safe error information."""

    with _tracer.start_as_current_span(
        name,
        kind=kind,
        attributes=attributes,
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        try:
            yield span
        except BaseException as error:
            _mark_span_error(span, error)
            raise


@contextmanager
def trace_turn(
    *,
    session_id: str,
    turn_number: int,
    provider: str,
    model: str,
) -> Iterator[Span]:
    """Trace and measure one complete user turn."""

    started_at = perf_counter()
    status = "success"
    error_type: str | None = None

    span_attributes: dict[str, AttributeValue] = {
        **_model_attributes(provider, model),
        "gen_ai.conversation.id": session_id,
        "kio1.turn.number": turn_number,
    }

    with trace_operation("kio1.turn", span_attributes) as span:
        try:
            yield span
        except BaseException as error:
            status = "error"
            error_type = type(error).__name__
            raise
        finally:
            instruments = _instruments
            if instruments is not None:
                metric_attributes = _model_attributes(provider, model)
                metric_attributes["kio1.status"] = status

                if error_type is not None:
                    metric_attributes["error.type"] = error_type

                elapsed_seconds = perf_counter() - started_at
                instruments.turns.add(1, metric_attributes)
                instruments.turn_duration.record(
                    elapsed_seconds,
                    metric_attributes,
                )

                # Contract mandatory metrics (kio_request_count,
                # kio_request_duration_ms, kio_request_error_count): one
                # "request" is one complete user turn, mirroring the
                # platform's kio-simulator request definition. kio.id is
                # attached explicitly (not just via the resource attribute),
                # matching the platform's kio-simulator convention.
                instruments.request_count.add(1, {"status": status, "kio.id": _kio_id})
                instruments.request_duration_ms.record(
                    elapsed_seconds * 1000, {"kio.id": _kio_id}
                )

                if error_type is not None:
                    instruments.request_error_count.add(
                        1, {"error_type": error_type, "kio.id": _kio_id}
                    )

                _record_kpi_snapshot(is_error=status == "error")


@contextmanager
def trace_provider_preload(
    *,
    provider: str,
    model: str,
) -> Iterator[Span]:
    """Trace and measure model preload or provider validation."""

    started_at = perf_counter()
    status = "success"
    error_type: str | None = None
    span_attributes = _model_attributes(provider, model)

    with trace_operation("kio1.provider.preload", span_attributes) as span:
        try:
            yield span
        except BaseException as error:
            status = "error"
            error_type = type(error).__name__
            raise
        finally:
            instruments = _instruments
            if instruments is not None:
                metric_attributes = _model_attributes(provider, model)
                metric_attributes["kio1.status"] = status

                if error_type is not None:
                    metric_attributes["error.type"] = error_type

                instruments.provider_preloads.add(1, metric_attributes)
                instruments.provider_preload_duration.record(
                    perf_counter() - started_at,
                    metric_attributes,
                )


@contextmanager
def trace_gen_ai_request(
    *,
    provider: str,
    model: str,
    session_id: str,
    turn_number: int,
    message_count: int,
    max_output_tokens: int,
    temperature: float,
) -> Iterator[Span]:
    """Trace and measure one non-streaming model request."""

    started_at = perf_counter()
    error_type: str | None = None

    span_attributes: dict[str, AttributeValue] = {
        **_model_attributes(provider, model),
        "gen_ai.operation.name": "chat",
        "gen_ai.conversation.id": session_id,
        "gen_ai.request.max_tokens": max_output_tokens,
        "gen_ai.request.temperature": temperature,
        "gen_ai.request.is_streaming": False,
        "gen_ai.output.type": "json",
        "kio1.turn.number": turn_number,
        "kio1.conversation.message_count": message_count,
    }

    instruments = _instruments
    if instruments is not None:
        instruments.conversation_messages.record(
            message_count,
            _model_attributes(provider, model),
        )

    with trace_operation(
        f"chat {model}",
        span_attributes,
        kind=SpanKind.CLIENT,
    ) as span:
        try:
            yield span
        except BaseException as error:
            error_type = type(error).__name__
            raise
        finally:
            instruments = _instruments
            if instruments is not None:
                metric_attributes = _model_attributes(provider, model)
                metric_attributes["gen_ai.operation.name"] = "chat"

                if error_type is not None:
                    metric_attributes["error.type"] = error_type

                instruments.gen_ai_operation_duration.record(
                    perf_counter() - started_at,
                    metric_attributes,
                )


def record_gen_ai_response(
    *,
    provider: str,
    request_model: str,
    input_tokens: int | None,
    output_tokens: int | None,
    response_model: str | None = None,
    response_id: str | None = None,
    finish_reason: str | None = None,
    truncated: bool = False,
    truncation_reason: str = "unknown",
) -> None:
    """Record model response metadata without recording response content."""

    span = trace.get_current_span()

    if span.is_recording():
        if response_model:
            span.set_attribute("gen_ai.response.model", response_model)

        if response_id:
            span.set_attribute("gen_ai.response.id", response_id[:256])

        if finish_reason:
            span.set_attribute(
                "gen_ai.response.finish_reasons",
                [finish_reason],
            )

        if type(input_tokens) is int and input_tokens >= 0:
            span.set_attribute("gen_ai.usage.input_tokens", input_tokens)

        if type(output_tokens) is int and output_tokens >= 0:
            span.set_attribute("gen_ai.usage.output_tokens", output_tokens)

        if truncated:
            span.set_attribute("kio1.response.truncated", True)

    instruments = _instruments
    if instruments is None:
        return

    metric_attributes = _model_attributes(provider, request_model)

    if response_model:
        metric_attributes["gen_ai.response.model"] = response_model

    if type(input_tokens) is int and input_tokens >= 0:
        input_attributes = dict(metric_attributes)
        input_attributes["gen_ai.token.type"] = "input"
        instruments.gen_ai_token_usage.record(
            input_tokens,
            input_attributes,
        )
        # Contract mandatory metric kio_llm_token_count (tag: direction).
        instruments.llm_token_count.add(
            input_tokens, {"direction": "input", "kio.id": _kio_id}
        )

    if type(output_tokens) is int and output_tokens >= 0:
        output_attributes = dict(metric_attributes)
        output_attributes["gen_ai.token.type"] = "output"
        instruments.gen_ai_token_usage.record(
            output_tokens,
            output_attributes,
        )
        instruments.llm_token_count.add(
            output_tokens, {"direction": "output", "kio.id": _kio_id}
        )

    total_tokens = 0
    if type(input_tokens) is int and input_tokens >= 0:
        total_tokens += input_tokens
    if type(output_tokens) is int and output_tokens >= 0:
        total_tokens += output_tokens

    if total_tokens > 0:
        # Contract mandatory metric kio_llm_cost_usd. This is an estimate
        # from a fixed per-provider coefficient (_COST_PER_1K_TOKENS_USD),
        # not real provider billing data.
        cost_usd = (total_tokens / 1000) * _COST_PER_1K_TOKENS_USD.get(provider, 0.0)
        if cost_usd > 0:
            instruments.llm_cost_usd.add(cost_usd, {"kio.id": _kio_id})

    if truncated:
        normalized_reason = (
            truncation_reason
            if truncation_reason in _ALLOWED_TRUNCATION_REASONS
            else "unknown"
        )
        truncation_attributes = dict(metric_attributes)
        truncation_attributes["kio1.truncation.reason"] = normalized_reason
        instruments.response_truncations.add(
            1,
            truncation_attributes,
        )


def record_session_started(*, provider: str, model: str) -> None:
    """Increment the session-started counter."""

    instruments = _instruments
    if instruments is not None:
        instruments.sessions_started.add(
            1,
            _model_attributes(provider, model),
        )
        # Contract mandatory metric kio_session_active_count. Best-effort:
        # if the process is killed before record_session_completed() runs,
        # this count will not be decremented until telemetry re-initializes.
        instruments.session_active_count.add(1, {"kio.id": _kio_id})


def record_session_completed(
    *,
    provider: str,
    model: str,
    success: bool,
) -> None:
    """Increment the session-completed counter."""

    instruments = _instruments
    if instruments is not None:
        metric_attributes = _model_attributes(provider, model)
        metric_attributes["kio1.status"] = "success" if success else "error"
        instruments.sessions_completed.add(1, metric_attributes)
        instruments.session_active_count.add(-1, {"kio.id": _kio_id})


def record_format_fallback() -> None:
    """Record use of the Python-literal response-format fallback."""

    instruments = _instruments
    if instruments is not None:
        instruments.format_fallbacks.add(1)

    span = trace.get_current_span()
    if span.is_recording():
        span.add_event("kio1.response.format_fallback")


def record_workflow_plan(
    *,
    workflow_id: str | None,
    execution_mode: str,
    step_count: int,
) -> None:
    """Record the structure of a successfully parsed workflow plan."""

    normalized_mode = (
        execution_mode if execution_mode in _ALLOWED_EXECUTION_MODES else "unknown"
    )

    instruments = _instruments
    if instruments is not None:
        metric_attributes: dict[str, AttributeValue] = {
            "kio1.workflow.execution_mode": normalized_mode,
        }
        instruments.workflow_plans.add(1, metric_attributes)
        instruments.workflow_step_count.record(
            step_count,
            metric_attributes,
        )

    span = trace.get_current_span()
    if span.is_recording():
        span.set_attribute(
            "kio1.workflow.execution_mode",
            normalized_mode,
        )
        span.set_attribute("kio1.workflow.step_count", step_count)

        if workflow_id:
            span.set_attribute("kio1.workflow.id", workflow_id[:128])


@contextmanager
def trace_workflow_execution(
    *,
    workflow_id: str,
    execution_mode: str,
    step_count: int,
    dispatchable_step_count: int,
) -> Iterator[Span]:
    """Trace and measure dispatching one whole workflow plan.

    Args:
        workflow_id: Plan identifier, attached as a span attribute only.
            Workflow ids are unbounded, so they must never become a metric
            label (Contract section 2.1, low-cardinality rule).
        execution_mode: Plan execution mode, normalized to a bounded enum.
        step_count: Number of steps in the plan.
        dispatchable_step_count: Number of steps that resolved to an agent.

    Yields:
        The active span.
    """
    normalized_mode = (
        execution_mode if execution_mode in _ALLOWED_EXECUTION_MODES else "unknown"
    )

    started_at = perf_counter()
    status = "success"

    span_attributes: dict[str, AttributeValue] = {
        "kio1.workflow.id": workflow_id[:128],
        "kio1.workflow.execution_mode": normalized_mode,
        "kio1.workflow.step_count": step_count,
        "kio1.workflow.dispatchable_step_count": dispatchable_step_count,
    }

    with trace_operation("kio1.workflow.execute", span_attributes) as span:
        try:
            yield span
        except BaseException:
            status = "error"
            raise
        finally:
            instruments = _instruments
            if instruments is not None:
                metric_attributes: dict[str, AttributeValue] = {
                    "kio1.workflow.execution_mode": normalized_mode,
                    "kio1.status": status,
                }
                instruments.workflow_executions.add(1, metric_attributes)
                instruments.workflow_execution_duration.record(
                    perf_counter() - started_at,
                    metric_attributes,
                )


@contextmanager
def trace_step_dispatch(
    *,
    workflow_id: str,
    step_id: str,
    agent_id: str,
    capability: str,
) -> Iterator[Span]:
    """Trace delivery of one execution message to one KIO agent.

    The span is a CLIENT span so the receiving agent's own server span
    becomes its child once agents propagate trace context.

    Args:
        workflow_id: Plan identifier, span attribute only.
        step_id: Step identifier within the plan, span attribute only.
        agent_id: Target agent, such as `KIO10`.
        capability: Capability requested from the agent.

    Yields:
        The active span.
    """
    span_attributes: dict[str, AttributeValue] = {
        "kio1.workflow.id": workflow_id[:128],
        "kio1.step.id": step_id[:64],
        "kio1.agent.id": agent_id,
        "kio1.step.capability": capability,
    }

    with trace_operation(
        f"dispatch {agent_id}",
        span_attributes,
        kind=SpanKind.CLIENT,
    ) as span:
        yield span


def record_step_result(
    *,
    agent_id: str,
    capability: str,
    status: str,
    duration_seconds: float,
) -> None:
    """Record the outcome of a single agent dispatch.

    Both `agent_id` (KIO2-KIO13) and `capability` come from bounded sets
    defined in KIO1's system prompt, so they are safe as metric labels.

    Args:
        agent_id: Target agent, such as `KIO10`.
        capability: Capability requested from the agent.
        status: Either `ok` or `error`.
        duration_seconds: Wall-clock dispatch duration in seconds.

    Returns:
        None.
    """
    instruments = _instruments
    if instruments is None:
        return

    metric_attributes: dict[str, AttributeValue] = {
        "kio1.agent.id": agent_id,
        "kio1.step.capability": capability,
        "kio1.status": status,
    }

    instruments.step_dispatches.add(1, metric_attributes)
    instruments.step_duration.record(duration_seconds, metric_attributes)

    span = trace.get_current_span()
    if span.is_recording():
        span.set_attribute("kio1.step.status", status)
