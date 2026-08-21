import logging
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.propagate import extract
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import SpanKind, Status, StatusCode

from core.config_loader import TelemetryConfig

logger = logging.getLogger(__name__)

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]

_INSTRUMENTATION_SCOPE = "kio1.service"

# Path segments that carry an identifier are replaced in span names, because a
# span name is a low-cardinality label. The identifier itself is still recorded
# as an attribute.
_MAX_SPAN_NAME_CHARS = 128

_tracer_provider: TracerProvider | None = None


def init_service_tracing(
    config: TelemetryConfig,
    *,
    service_name: str,
) -> None:
    """Set up trace export for one of KIO1's services.

    Traces only, deliberately. The mandatory contract metrics are emitted by
    the orchestrator with `kio.id = kio1`; emitting them from a second process
    under the same id would double-count every counter.

    Args:
        config: Validated telemetry configuration, reused so a service does
            not need its own endpoint and token settings.
        service_name: Distinguishes this service from the orchestrator in the
            platform's dashboards, for example `kio1-session-manager`.

    Returns:
        None.
    """
    global _tracer_provider

    if not config.enabled:
        logger.info("OpenTelemetry export is disabled")
        return

    if _tracer_provider is not None:
        logger.debug("Service tracing is already initialized")
        return

    resource = Resource.create(
        {
            "service.name": service_name,
            "kio.id": config.kio_id,
            "deployment.environment": config.deployment_environment,
        }
    )

    headers = (
        {"Authorization": f"Bearer {config.otlp_bearer_token}"}
        if config.otlp_bearer_token
        else None
    )

    provider = TracerProvider(
        resource=resource,
        sampler=ParentBased(TraceIdRatioBased(config.trace_sample_ratio)),
        shutdown_on_exit=False,
    )
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(
                endpoint=f"{config.otlp_http_endpoint}/v1/traces",
                headers=headers,
            )
        )
    )

    trace.set_tracer_provider(provider)
    _tracer_provider = provider

    logger.info(
        "Service tracing initialized: service=%s endpoint=%s",
        service_name,
        config.otlp_http_endpoint,
    )


def shutdown_service_tracing() -> None:
    """Flush pending spans and shut the exporter down."""
    global _tracer_provider

    provider, _tracer_provider = _tracer_provider, None

    if provider is None:
        return

    try:
        provider.shutdown()
    except Exception:
        logger.exception("Failed to shut down service tracing")


class TraceContextMiddleware:
    """Continues the caller's trace and records one SERVER span per request.

    KIO1 sends a W3C `traceparent` header on every call it makes. Without a
    middleware reading it, a service's own work starts a separate, unconnected
    trace and the end-to-end path breaks at that boundary.

    Written as plain ASGI rather than pulling in an instrumentation package,
    matching the way the rest of the project avoids dependencies it can do
    without.
    """

    def __init__(self, app: Any, *, service_name: str) -> None:
        """Wrap an ASGI application.

        Args:
            app: The application to wrap.
            service_name: Recorded on every span so a trace shows which
                service handled the request.
        """
        self._app = app
        self._service_name = service_name

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle one ASGI event, tracing HTTP requests.

        Args:
            scope: The ASGI scope.
            receive: The ASGI receive callable.
            send: The ASGI send callable.

        Returns:
            None.
        """
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        tracer = trace.get_tracer(_INSTRUMENTATION_SCOPE)
        method = str(scope.get("method", "GET"))
        path = str(scope.get("path", "/"))
        context = extract(_headers_of(scope))
        status_code: int | None = None

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code

            if message.get("type") == "http.response.start":
                status_code = int(message.get("status", 0))

            await send(message)

        with tracer.start_as_current_span(
            f"{method} {path}"[:_MAX_SPAN_NAME_CHARS],
            context=context,
            kind=SpanKind.SERVER,
            attributes={
                "http.request.method": method,
                "url.path": path,
                "service.name": self._service_name,
            },
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                await self._app(scope, receive, send_wrapper)
            except BaseException as error:
                span.set_attribute("error.type", type(error).__name__)
                span.set_status(Status(StatusCode.ERROR))
                raise
            finally:
                # The route template is only known once Starlette has matched
                # it, so the span is renamed here rather than at creation. A
                # raw path would put a session id in the span name, which must
                # stay low-cardinality.
                route = _route_template(scope)

                if route is not None:
                    span.update_name(f"{method} {route}"[:_MAX_SPAN_NAME_CHARS])

                session_id = _session_id_of(scope)

                if session_id is not None:
                    span.set_attribute("session.id", session_id)

                if status_code is not None:
                    span.set_attribute("http.response.status_code", status_code)

                    if status_code >= 500:
                        span.set_status(Status(StatusCode.ERROR))


def _headers_of(scope: Scope) -> dict[str, str]:
    """Return the request headers as a text mapping for context extraction.

    Args:
        scope: The ASGI scope.

    Returns:
        Lowercased header names mapped to their decoded values.
    """
    headers: dict[str, str] = {}

    for raw_name, raw_value in scope.get("headers", []):
        headers[raw_name.decode("latin-1").lower()] = raw_value.decode("latin-1")

    return headers


def _route_template(scope: Scope) -> str | None:
    """Return the matched route pattern, when routing has determined one.

    Args:
        scope: The ASGI scope.

    Returns:
        The route path template, or None when nothing matched.
    """
    route = scope.get("route")
    template = getattr(route, "path", None)

    return template if isinstance(template, str) else None


def _session_id_of(scope: Scope) -> str | None:
    """Return the session id from the matched path parameters, if any.

    Args:
        scope: The ASGI scope.

    Returns:
        The session id as text, or None when the route carries none.
    """
    parameters = scope.get("path_params")

    if not isinstance(parameters, dict):
        return None

    session_id = parameters.get("session_id")

    return str(session_id) if session_id is not None else None
