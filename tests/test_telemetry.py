import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest
from opentelemetry.trace import StatusCode

from core import telemetry
from core.config_loader import Config, TelemetryConfig
from core.formatter import format_json
from providers.anthropic_client import send_request as send_anthropic_request
from providers.ollama_client import send_request as send_ollama_request
from providers.openai_client import send_request as send_openai_request


def _recording_tracer() -> tuple[MagicMock, MagicMock]:
    """Create a mocked tracer returning a recording span."""

    span = MagicMock()
    span.is_recording.return_value = True

    span_context = MagicMock()
    span_context.__enter__.return_value = span
    span_context.__exit__.return_value = False

    tracer = MagicMock()
    tracer.start_as_current_span.return_value = span_context

    return tracer, span


def _provider_config(provider: str) -> Config:
    """Create provider configuration for telemetry mapping tests."""

    provider_options: dict[str, Any] = {}

    if provider == "ollama":
        provider_options = {
            "endpoint": "http://localhost:11434",
            "context_window_size": 16384,
        }

    return Config(
        provider=provider,
        allowed_providers=frozenset({"ollama", "openai", "anthropic"}),
        model="requested-model",
        prompt_path="prompt.txt",
        chat_directory="chats",
        temperature=0.1,
        request_timeout=120,
        keep_alive=-1,
        max_output_tokens=1024,
        provider_options=provider_options,
    )


def test_interrupted_turn_is_recorded_as_error() -> None:
    """Verify an interrupted request is not reported as successful."""

    tracer, span = _recording_tracer()
    instruments = SimpleNamespace(
        turns=MagicMock(),
        turn_duration=MagicMock(),
        request_count=MagicMock(),
        request_duration_ms=MagicMock(),
        request_error_count=MagicMock(),
    )

    with (
        patch.object(telemetry, "_tracer", tracer),
        patch.object(telemetry, "_instruments", instruments),
        patch.object(telemetry, "_record_kpi_snapshot"),
        patch.object(
            telemetry,
            "perf_counter",
            side_effect=[10.0, 11.0],
        ),
    ):
        with pytest.raises(KeyboardInterrupt):
            with telemetry.trace_turn(
                session_id="session-123",
                turn_number=1,
                provider="ollama",
                model="requested-model",
            ):
                raise KeyboardInterrupt()

    _, metric_attributes = instruments.turns.add.call_args.args

    assert metric_attributes["kio1.status"] == "error"
    assert metric_attributes["error.type"] == "KeyboardInterrupt"
    span.set_attribute.assert_any_call(
        "error.type",
        "KeyboardInterrupt",
    )


@patch("providers.ollama_client.record_gen_ai_response")
@patch("providers.ollama_client.urllib.request.urlopen")
def test_ollama_maps_response_telemetry_before_truncation(
    urlopen: MagicMock,
    record_response: MagicMock,
) -> None:
    """Verify Ollama response fields are normalized before raising."""

    response = MagicMock()
    response.read.return_value = json.dumps(
        {
            "model": "returned-model",
            "message": {"content": "{}"},
            "done_reason": "length",
            "prompt_eval_count": 3432,
            "eval_count": 664,
        }
    ).encode("utf-8")
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    urlopen.return_value = response

    config = _provider_config("ollama")

    with pytest.raises(ValueError, match="Response truncated"):
        send_ollama_request(config, None, "system", [])

    record_response.assert_called_once_with(
        provider="ollama",
        request_model="requested-model",
        input_tokens=3432,
        output_tokens=664,
        response_model="returned-model",
        finish_reason="length",
        truncated=True,
        truncation_reason="length",
    )


@patch("providers.openai_client.record_gen_ai_response")
def test_openai_maps_response_telemetry_before_truncation(
    record_response: MagicMock,
) -> None:
    """Verify OpenAI response fields are normalized before raising."""

    response = SimpleNamespace(
        id="response-openai",
        model="returned-model",
        usage=SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=25,
        ),
        choices=[SimpleNamespace(finish_reason="length")],
    )
    client = MagicMock()
    client.chat.completions.create.return_value = response

    config = _provider_config("openai")

    with pytest.raises(ValueError, match="max_output_tokens"):
        send_openai_request(config, client, "system", [])

    record_response.assert_called_once_with(
        provider="openai",
        request_model="requested-model",
        input_tokens=100,
        output_tokens=25,
        response_model="returned-model",
        response_id="response-openai",
        finish_reason="length",
        truncated=True,
        truncation_reason="max_output_tokens",
    )


@patch("providers.anthropic_client.record_gen_ai_response")
def test_anthropic_maps_response_telemetry_before_truncation(
    record_response: MagicMock,
) -> None:
    """Verify Anthropic response fields are normalized before raising."""

    response = SimpleNamespace(
        id="response-anthropic",
        model="returned-model",
        usage=SimpleNamespace(
            input_tokens=120,
            output_tokens=30,
        ),
        stop_reason="max_tokens",
    )
    client = MagicMock()
    client.messages.create.return_value = response

    config = _provider_config("anthropic")

    with pytest.raises(ValueError, match="max_output_tokens"):
        send_anthropic_request(config, client, "system", [])

    record_response.assert_called_once_with(
        provider="anthropic",
        request_model="requested-model",
        input_tokens=120,
        output_tokens=30,
        response_model="returned-model",
        response_id="response-anthropic",
        finish_reason="max_tokens",
        truncated=True,
        truncation_reason="max_output_tokens",
    )


def test_disabled_telemetry_does_not_create_exporters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify disabled telemetry performs no exporter initialization."""

    monkeypatch.setattr(telemetry, "_tracer_provider", None)
    monkeypatch.setattr(telemetry, "_meter_provider", None)

    with (
        patch.object(telemetry, "OTLPSpanExporter") as span_exporter,
        patch.object(telemetry, "OTLPMetricExporter") as metric_exporter,
    ):
        telemetry.init_telemetry(TelemetryConfig(), llm="test-model")

    span_exporter.assert_not_called()
    metric_exporter.assert_not_called()


def test_init_telemetry_builds_otlp_signal_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify traces and metrics use their respective OTLP HTTP paths."""

    for attribute_name in (
        "_tracer_provider",
        "_meter_provider",
        "_tracer",
        "_meter",
        "_instruments",
    ):
        monkeypatch.setattr(
            telemetry,
            attribute_name,
            getattr(telemetry, attribute_name),
        )

    monkeypatch.setattr(telemetry, "_tracer_provider", None)
    monkeypatch.setattr(telemetry, "_meter_provider", None)

    span_exporter = MagicMock()
    metric_exporter = MagicMock()
    span_processor = MagicMock()
    metric_reader = MagicMock()
    tracer_provider = MagicMock()
    meter_provider = MagicMock()
    tracer = MagicMock()
    meter = MagicMock()
    instruments = MagicMock()

    tracer_provider.get_tracer.return_value = tracer
    meter_provider.get_meter.return_value = meter

    with (
        patch.object(
            telemetry,
            "OTLPSpanExporter",
            return_value=span_exporter,
        ) as span_exporter_factory,
        patch.object(
            telemetry,
            "OTLPMetricExporter",
            return_value=metric_exporter,
        ) as metric_exporter_factory,
        patch.object(
            telemetry,
            "BatchSpanProcessor",
            return_value=span_processor,
        ) as span_processor_factory,
        patch.object(
            telemetry,
            "PeriodicExportingMetricReader",
            return_value=metric_reader,
        ) as metric_reader_factory,
        patch.object(
            telemetry,
            "TracerProvider",
            return_value=tracer_provider,
        ) as tracer_provider_factory,
        patch.object(
            telemetry,
            "MeterProvider",
            return_value=meter_provider,
        ) as meter_provider_factory,
        patch.object(telemetry.trace, "set_tracer_provider") as set_tracer_provider,
        patch.object(telemetry.metrics, "set_meter_provider") as set_meter_provider,
        patch.object(
            telemetry,
            "_create_instruments",
            return_value=instruments,
        ) as create_instruments,
    ):
        telemetry.init_telemetry(
            TelemetryConfig(
                enabled=True,
                service_name="test-orchestrator",
                otlp_http_endpoint="http://collector:4318",
                otlp_bearer_token="test-token",
                metric_export_interval_ms=2500,
                trace_sample_ratio=0.25,
                kio_id="kio1",
            ),
            llm="test-model",
            task_type="orchestration",
        )

    span_exporter_factory.assert_called_once_with(
        endpoint="http://collector:4318/v1/traces",
        headers={"Authorization": "Bearer test-token"},
    )
    metric_exporter_factory.assert_called_once_with(
        endpoint="http://collector:4318/v1/metrics",
        headers={"Authorization": "Bearer test-token"},
    )

    span_processor_factory.assert_called_once_with(span_exporter)
    tracer_provider.add_span_processor.assert_called_once_with(span_processor)

    metric_reader_factory.assert_called_once_with(
        metric_exporter,
        export_interval_millis=2500,
    )

    tracer_provider_arguments = tracer_provider_factory.call_args.kwargs
    assert tracer_provider_arguments["shutdown_on_exit"] is False
    resource_attributes = tracer_provider_arguments["resource"].attributes
    assert resource_attributes["service.name"] == "test-orchestrator"
    assert resource_attributes["kio.id"] == "kio1"
    assert resource_attributes["llm"] == "test-model"
    assert resource_attributes["task_type"] == "orchestration"

    meter_provider_factory.assert_called_once_with(
        resource=tracer_provider_arguments["resource"],
        metric_readers=[metric_reader],
        shutdown_on_exit=False,
    )

    set_tracer_provider.assert_called_once_with(tracer_provider)
    set_meter_provider.assert_called_once_with(meter_provider)
    create_instruments.assert_called_once_with(meter)

    assert telemetry._tracer is tracer
    assert telemetry._meter is meter
    assert telemetry._instruments is instruments


def test_init_telemetry_omits_auth_header_without_bearer_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify exporters get no Authorization header when the token is unset."""

    for attribute_name in (
        "_tracer_provider",
        "_meter_provider",
        "_tracer",
        "_meter",
        "_instruments",
    ):
        monkeypatch.setattr(
            telemetry,
            attribute_name,
            getattr(telemetry, attribute_name),
        )

    monkeypatch.setattr(telemetry, "_tracer_provider", None)
    monkeypatch.setattr(telemetry, "_meter_provider", None)

    with (
        patch.object(telemetry, "OTLPSpanExporter") as span_exporter_factory,
        patch.object(telemetry, "OTLPMetricExporter") as metric_exporter_factory,
        patch.object(telemetry, "TracerProvider") as tracer_provider_factory,
        patch.object(telemetry, "MeterProvider") as meter_provider_factory,
        patch.object(telemetry.trace, "set_tracer_provider"),
        patch.object(telemetry.metrics, "set_meter_provider"),
    ):
        tracer_provider_factory.return_value.get_tracer.return_value = MagicMock()
        meter_provider_factory.return_value.get_meter.return_value = MagicMock()

        telemetry.init_telemetry(
            TelemetryConfig(enabled=True),
            llm="test-model",
        )

    assert span_exporter_factory.call_args.kwargs["headers"] is None
    assert metric_exporter_factory.call_args.kwargs["headers"] is None


def test_trace_operation_marks_errors_without_recording_messages() -> None:
    """Verify spans receive only the exception type and error status."""

    tracer, span = _recording_tracer()

    with patch.object(telemetry, "_tracer", tracer):
        with pytest.raises(ValueError, match="sensitive error text"):
            with telemetry.trace_operation("test.operation"):
                raise ValueError("sensitive error text")

    span.set_attribute.assert_called_once_with("error.type", "ValueError")
    span.record_exception.assert_not_called()

    status = span.set_status.call_args.args[0]
    assert status.status_code is StatusCode.ERROR

    start_arguments = tracer.start_as_current_span.call_args.kwargs
    assert start_arguments["record_exception"] is False
    assert start_arguments["set_status_on_exception"] is False


def test_turn_metrics_exclude_session_identifiers() -> None:
    """Verify session IDs remain in traces and never become metric labels."""

    tracer, _ = _recording_tracer()
    instruments = SimpleNamespace(
        turns=MagicMock(),
        turn_duration=MagicMock(),
        request_count=MagicMock(),
        request_duration_ms=MagicMock(),
        request_error_count=MagicMock(),
    )

    with (
        patch.object(telemetry, "_tracer", tracer),
        patch.object(telemetry, "_instruments", instruments),
        patch.object(telemetry, "_record_kpi_snapshot"),
        patch.object(
            telemetry,
            "perf_counter",
            side_effect=[10.0, 12.5],
        ),
    ):
        with telemetry.trace_turn(
            session_id="session-123",
            turn_number=4,
            provider="ollama",
            model="test-model",
        ):
            pass

    instruments.turns.add.assert_called_once()
    _, turn_attributes = instruments.turns.add.call_args.args

    assert turn_attributes == {
        "gen_ai.provider.name": "ollama",
        "gen_ai.request.model": "test-model",
        "kio1.status": "success",
    }
    assert "gen_ai.conversation.id" not in turn_attributes
    assert "kio1.turn.number" not in turn_attributes

    instruments.turn_duration.record.assert_called_once_with(
        2.5,
        turn_attributes,
    )

    span_attributes = tracer.start_as_current_span.call_args.kwargs["attributes"]
    assert span_attributes["gen_ai.conversation.id"] == "session-123"
    assert span_attributes["kio1.turn.number"] == 4


def test_record_gen_ai_response_records_tokens_and_truncation() -> None:
    """Verify normalized model-response metrics and trace attributes."""

    span = MagicMock()
    span.is_recording.return_value = True

    instruments = SimpleNamespace(
        gen_ai_token_usage=MagicMock(),
        response_truncations=MagicMock(),
        llm_token_count=MagicMock(),
        llm_cost_usd=MagicMock(),
    )

    with (
        patch.object(
            telemetry.trace,
            "get_current_span",
            return_value=span,
        ),
        patch.object(telemetry, "_instruments", instruments),
    ):
        telemetry.record_gen_ai_response(
            provider="ollama",
            request_model="requested-model",
            input_tokens=21,
            output_tokens=8,
            response_model="returned-model",
            response_id="response-123",
            finish_reason="length",
            truncated=True,
            truncation_reason="length",
        )

    base_attributes = {
        "gen_ai.provider.name": "ollama",
        "gen_ai.request.model": "requested-model",
        "gen_ai.response.model": "returned-model",
    }

    assert instruments.gen_ai_token_usage.record.call_args_list == [
        call(
            21,
            {
                **base_attributes,
                "gen_ai.token.type": "input",
            },
        ),
        call(
            8,
            {
                **base_attributes,
                "gen_ai.token.type": "output",
            },
        ),
    ]

    instruments.response_truncations.add.assert_called_once_with(
        1,
        {
            **base_attributes,
            "kio1.truncation.reason": "length",
        },
    )

    span.set_attribute.assert_any_call(
        "gen_ai.response.finish_reasons",
        ["length"],
    )
    span.set_attribute.assert_any_call("gen_ai.usage.input_tokens", 21)
    span.set_attribute.assert_any_call("gen_ai.usage.output_tokens", 8)
    span.set_attribute.assert_any_call("kio1.response.truncated", True)


def test_workflow_metrics_bound_execution_mode_and_exclude_id() -> None:
    """Verify workflow identifiers are trace-only and labels stay bounded."""

    span = MagicMock()
    span.is_recording.return_value = True

    instruments = SimpleNamespace(
        workflow_plans=MagicMock(),
        workflow_step_count=MagicMock(),
    )
    workflow_id = "wf-" + ("x" * 200)

    with (
        patch.object(
            telemetry.trace,
            "get_current_span",
            return_value=span,
        ),
        patch.object(telemetry, "_instruments", instruments),
    ):
        telemetry.record_workflow_plan(
            workflow_id=workflow_id,
            execution_mode="unexpected-mode",
            step_count=3,
        )

    metric_attributes = {
        "kio1.workflow.execution_mode": "unknown",
    }
    instruments.workflow_plans.add.assert_called_once_with(
        1,
        metric_attributes,
    )
    instruments.workflow_step_count.record.assert_called_once_with(
        3,
        metric_attributes,
    )

    assert "kio1.workflow.id" not in metric_attributes
    span.set_attribute.assert_any_call(
        "kio1.workflow.id",
        workflow_id[:128],
    )


def test_formatter_records_fallback_and_workflow_structure() -> None:
    """Verify formatter telemetry excludes explanation and step content."""

    raw_response = (
        "{'workflow_id': 'wf-1', "
        "'execution_mode': 'parallel', "
        "'steps': [{}, {}], "
        "'explanation': 'do not export this'}"
    )

    with (
        patch("core.formatter.record_format_fallback") as record_fallback,
        patch("core.formatter.record_workflow_plan") as record_plan,
    ):
        formatted = format_json(raw_response)

    assert '"workflow_id": "wf-1"' in formatted
    record_fallback.assert_called_once_with()
    record_plan.assert_called_once_with(
        workflow_id="wf-1",
        execution_mode="parallel",
        step_count=2,
    )


def test_shutdown_continues_when_metric_shutdown_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify trace shutdown still runs after a metric shutdown error."""

    meter_provider = MagicMock()
    meter_provider.shutdown.side_effect = RuntimeError("metric shutdown failed")
    tracer_provider = MagicMock()

    monkeypatch.setattr(
        telemetry,
        "_meter_provider",
        meter_provider,
    )
    monkeypatch.setattr(
        telemetry,
        "_tracer_provider",
        tracer_provider,
    )

    telemetry.shutdown_telemetry()

    meter_provider.shutdown.assert_called_once_with()
    tracer_provider.shutdown.assert_called_once_with()


def test_heartbeat_loop_ticks_until_stopped() -> None:
    """Verify the heartbeat loop increments the counter once per interval."""

    instruments = SimpleNamespace(heartbeat=MagicMock())
    stop_event = MagicMock()
    stop_event.wait.side_effect = [False, False, True]

    telemetry._heartbeat_loop(instruments, stop_event, "kio1")

    assert instruments.heartbeat.add.call_count == 2
    instruments.heartbeat.add.assert_called_with(1, {"kio.id": "kio1"})
    stop_event.wait.assert_called_with(telemetry._HEARTBEAT_INTERVAL_SECONDS)


def test_start_heartbeat_replaces_existing_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify restarting the heartbeat stops the previous thread first."""

    instruments = SimpleNamespace(heartbeat=MagicMock())
    monkeypatch.setattr(telemetry, "_heartbeat_thread", None)
    monkeypatch.setattr(telemetry, "_heartbeat_stop_event", None)
    monkeypatch.setattr(telemetry, "_HEARTBEAT_INTERVAL_SECONDS", 3600.0)

    try:
        telemetry._start_heartbeat(instruments, "kio1")
        first_thread = telemetry._heartbeat_thread
        assert first_thread is not None
        assert first_thread.is_alive()

        telemetry._start_heartbeat(instruments, "kio1")
        second_thread = telemetry._heartbeat_thread
        assert second_thread is not first_thread
        assert not first_thread.is_alive()
        assert second_thread.is_alive()
    finally:
        telemetry._stop_heartbeat()

    assert telemetry._heartbeat_thread is None
    assert telemetry._heartbeat_stop_event is None


def test_trace_turn_records_contract_request_metrics() -> None:
    """Verify a successful turn records the contract's request metrics."""

    tracer, _ = _recording_tracer()
    instruments = SimpleNamespace(
        turns=MagicMock(),
        turn_duration=MagicMock(),
        request_count=MagicMock(),
        request_duration_ms=MagicMock(),
        request_error_count=MagicMock(),
    )

    with (
        patch.object(telemetry, "_tracer", tracer),
        patch.object(telemetry, "_instruments", instruments),
        patch.object(telemetry, "_kio_id", "kio1"),
        patch.object(telemetry, "_record_kpi_snapshot"),
        patch.object(
            telemetry,
            "perf_counter",
            side_effect=[10.0, 10.4],
        ),
    ):
        with telemetry.trace_turn(
            session_id="session-123",
            turn_number=1,
            provider="ollama",
            model="test-model",
        ):
            pass

    instruments.request_count.add.assert_called_once_with(
        1, {"status": "success", "kio.id": "kio1"}
    )
    instruments.request_duration_ms.record.assert_called_once_with(
        pytest.approx(400.0), {"kio.id": "kio1"}
    )
    instruments.request_error_count.add.assert_not_called()


def test_trace_turn_records_contract_error_metrics() -> None:
    """Verify a failed turn records the contract's error metric."""

    tracer, _ = _recording_tracer()
    instruments = SimpleNamespace(
        turns=MagicMock(),
        turn_duration=MagicMock(),
        request_count=MagicMock(),
        request_duration_ms=MagicMock(),
        request_error_count=MagicMock(),
    )

    with (
        patch.object(telemetry, "_tracer", tracer),
        patch.object(telemetry, "_instruments", instruments),
        patch.object(telemetry, "_kio_id", "kio1"),
        patch.object(telemetry, "_record_kpi_snapshot"),
        patch.object(
            telemetry,
            "perf_counter",
            side_effect=[10.0, 10.1],
        ),
        pytest.raises(RuntimeError),
    ):
        with telemetry.trace_turn(
            session_id="session-123",
            turn_number=1,
            provider="ollama",
            model="test-model",
        ):
            raise RuntimeError("boom")

    instruments.request_count.add.assert_called_once_with(
        1, {"status": "error", "kio.id": "kio1"}
    )
    instruments.request_error_count.add.assert_called_once_with(
        1, {"error_type": "RuntimeError", "kio.id": "kio1"}
    )


def test_record_gen_ai_response_records_contract_token_and_cost_metrics() -> None:
    """Verify token direction counts and an estimated cost are recorded."""

    span = MagicMock()
    span.is_recording.return_value = False

    instruments = SimpleNamespace(
        gen_ai_token_usage=MagicMock(),
        response_truncations=MagicMock(),
        llm_token_count=MagicMock(),
        llm_cost_usd=MagicMock(),
    )

    with (
        patch.object(
            telemetry.trace,
            "get_current_span",
            return_value=span,
        ),
        patch.object(telemetry, "_instruments", instruments),
        patch.object(telemetry, "_kio_id", "kio1"),
    ):
        telemetry.record_gen_ai_response(
            provider="openai",
            request_model="requested-model",
            input_tokens=750,
            output_tokens=250,
        )

    instruments.llm_token_count.add.assert_any_call(
        750, {"direction": "input", "kio.id": "kio1"}
    )
    instruments.llm_token_count.add.assert_any_call(
        250, {"direction": "output", "kio.id": "kio1"}
    )
    instruments.llm_cost_usd.add.assert_called_once_with(
        pytest.approx(0.002), {"kio.id": "kio1"}
    )


def test_record_gen_ai_response_skips_cost_for_free_provider() -> None:
    """Verify no cost is recorded for a provider with a zero coefficient."""

    span = MagicMock()
    span.is_recording.return_value = False

    instruments = SimpleNamespace(
        gen_ai_token_usage=MagicMock(),
        response_truncations=MagicMock(),
        llm_token_count=MagicMock(),
        llm_cost_usd=MagicMock(),
    )

    with (
        patch.object(
            telemetry.trace,
            "get_current_span",
            return_value=span,
        ),
        patch.object(telemetry, "_instruments", instruments),
    ):
        telemetry.record_gen_ai_response(
            provider="ollama",
            request_model="requested-model",
            input_tokens=100,
            output_tokens=50,
        )

    instruments.llm_cost_usd.add.assert_not_called()


def test_session_started_and_completed_track_active_count() -> None:
    """Verify session start/completion increments and decrements active count."""

    instruments = SimpleNamespace(
        sessions_started=MagicMock(),
        sessions_completed=MagicMock(),
        session_active_count=MagicMock(),
    )

    with (
        patch.object(telemetry, "_instruments", instruments),
        patch.object(telemetry, "_kio_id", "kio1"),
    ):
        telemetry.record_session_started(provider="ollama", model="test-model")
        telemetry.record_session_completed(
            provider="ollama",
            model="test-model",
            success=True,
        )

    instruments.session_active_count.add.assert_has_calls(
        [call(1, {"kio.id": "kio1"}), call(-1, {"kio.id": "kio1"})]
    )


_KPI_INSTRUMENT_NAMES = [
    "kpi_codegen_duration",
    "kpi_issue_resolution",
    "kpi_lifecycle_energy",
    "kpi_deploy_energy_efficiency",
    "kpi_code_quality",
    "kpi_review_score",
    "kpi_dev_productivity",
    "kpi_time_to_market",
    "kpi_bugfix_duration",
    "kpi_customer_reported_issues",
    "kpi_cost_saving",
    "kpi_adoption_rate",
    "kpi_adoption_usage",
    "kpi_adoption_mos",
    "kpi_cross_arch_build",
    "kpi_refactoring",
    "kpi_tech_debt",
]


def test_record_kpi_snapshot_records_every_d11_kpi() -> None:
    """Verify every D1.1 KPI histogram is recorded, labeled as simulated."""

    instruments = SimpleNamespace(
        **{name: MagicMock() for name in _KPI_INSTRUMENT_NAMES}
    )
    labels = {"kio.id": "kio1", "source": "simulated"}

    with (
        patch.object(telemetry, "_instruments", instruments),
        patch.object(telemetry, "_kio_id", "kio1"),
        patch.object(telemetry.random, "uniform", return_value=1.0),
        # Keep the rare, event-style KPIs (6.2, 8.3) from firing here; their
        # gating is covered by test_record_kpi_snapshot_gated_events_fire_*.
        patch.object(telemetry.random, "random", return_value=0.99),
    ):
        telemetry._record_kpi_snapshot(is_error=False)

    for histogram_name in (
        "kpi_codegen_duration",
        "kpi_issue_resolution",
        "kpi_lifecycle_energy",
        "kpi_deploy_energy_efficiency",
        "kpi_code_quality",
        "kpi_review_score",
        "kpi_dev_productivity",
        "kpi_time_to_market",
        "kpi_bugfix_duration",
        "kpi_cost_saving",
        "kpi_adoption_rate",
        "kpi_adoption_usage",
        "kpi_adoption_mos",
        "kpi_refactoring",
        "kpi_tech_debt",
    ):
        getattr(instruments, histogram_name).record.assert_called_once_with(1.0, labels)

    instruments.kpi_customer_reported_issues.add.assert_not_called()
    instruments.kpi_cross_arch_build.add.assert_not_called()


def test_record_kpi_snapshot_gated_events_fire_on_low_random_and_error() -> None:
    """Verify the rare event-style KPIs (6.2, 8.3) fire when their gate opens."""

    instruments = SimpleNamespace(
        **{name: MagicMock() for name in _KPI_INSTRUMENT_NAMES}
    )
    labels = {"kio.id": "kio1", "source": "simulated"}

    with (
        patch.object(telemetry, "_instruments", instruments),
        patch.object(telemetry, "_kio_id", "kio1"),
        patch.object(telemetry.random, "uniform", return_value=1.0),
        patch.object(telemetry.random, "random", return_value=0.01),
        patch.object(telemetry.random, "randint", return_value=2),
    ):
        telemetry._record_kpi_snapshot(is_error=True)

    instruments.kpi_customer_reported_issues.add.assert_called_once_with(2, labels)
    instruments.kpi_cross_arch_build.add.assert_called_once_with(1, labels)


def test_record_kpi_snapshot_skips_customer_reported_when_not_an_error() -> None:
    """Verify KPI 6.2 never fires on a successful turn, even if the gate opens."""

    instruments = SimpleNamespace(
        **{name: MagicMock() for name in _KPI_INSTRUMENT_NAMES}
    )

    with (
        patch.object(telemetry, "_instruments", instruments),
        patch.object(telemetry, "_kio_id", "kio1"),
        patch.object(telemetry.random, "uniform", return_value=1.0),
        patch.object(telemetry.random, "random", return_value=0.01),
    ):
        telemetry._record_kpi_snapshot(is_error=False)

    instruments.kpi_customer_reported_issues.add.assert_not_called()
