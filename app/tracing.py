"""
OpenTelemetry distributed tracing — optional, no-op when disabled or uninstalled.

Provides span helpers that wrap the 7 pipeline steps, LLM provider calls,
and circuit breaker events. When OTEL_ENABLED=false (default) or the
opentelemetry SDK is not installed, all operations are zero-cost no-ops.

Usage:
    from app.tracing import get_tracer, span

    tracer = get_tracer(__name__)
    with span(tracer, "step_name", attributes={"key": "val"}):
        ...
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)

_tracer_provider: Any = None
_initialized = False

try:
    from opentelemetry import trace
    from opentelemetry.trace import StatusCode

    _HAS_OTEL = True
except ImportError:
    _HAS_OTEL = False
    trace = None
    StatusCode = None


def init_tracing() -> None:
    """Initialize OTel tracer provider and OTLP exporter. Call once at startup."""
    global _tracer_provider, _initialized

    if _initialized:
        return

    from app.config import settings

    if not settings.OTEL_ENABLED or not _HAS_OTEL:
        _initialized = True
        if settings.OTEL_ENABLED and not _HAS_OTEL:
            logger.warning("OTEL_ENABLED=true but opentelemetry not installed; tracing disabled")
        return

    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.sampling import TraceIdRatioBased

    resource = Resource.create({
        "service.name": settings.OTEL_SERVICE_NAME,
        "service.version": "1.0.0",
        "deployment.environment": settings.ENVIRONMENT,
    })
    sampler = TraceIdRatioBased(settings.OTEL_SAMPLE_RATE)
    _tracer_provider = TracerProvider(resource=resource, sampler=sampler)

    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        exporter = OTLPSpanExporter(endpoint=f"{settings.OTEL_EXPORTER_ENDPOINT}/v1/traces")
        _tracer_provider.add_span_processor(BatchSpanProcessor(exporter))
        logger.info("OpenTelemetry tracing initialized: endpoint=%s", settings.OTEL_EXPORTER_ENDPOINT)
    except Exception:
        logger.exception("Failed to configure OTLP exporter; spans will not be exported")

    trace.set_tracer_provider(_tracer_provider)
    _initialized = True


def shutdown_tracing() -> None:
    """Flush pending spans and shut down the tracer provider."""
    global _tracer_provider, _initialized
    if _tracer_provider is not None:
        try:
            _tracer_provider.shutdown()
        except Exception:
            logger.debug("Tracer provider shutdown error", exc_info=True)
    _tracer_provider = None
    _initialized = False


def get_tracer(name: str) -> Any:
    """Get a tracer instance. Returns a no-op tracer if OTel is disabled."""
    if not _HAS_OTEL:
        return _NoopTracer()
    from app.config import settings

    if not settings.OTEL_ENABLED:
        return _NoopTracer()
    return trace.get_tracer(name)


@contextmanager
def span(
    tracer: Any,
    name: str,
    attributes: dict[str, Any] | None = None,
) -> Any:
    """Context manager that creates a span or no-ops gracefully."""
    if isinstance(tracer, _NoopTracer):
        yield _NoopSpan()
        return

    with tracer.start_as_current_span(name, attributes=attributes or {}) as s:
        try:
            yield s
        except Exception as exc:
            if _HAS_OTEL and StatusCode is not None:
                s.set_status(StatusCode.ERROR, str(exc))
                s.record_exception(exc)
            raise


def set_span_attributes(s: Any, attrs: dict[str, Any]) -> None:
    """Set attributes on a span (no-op safe)."""
    if isinstance(s, _NoopSpan):
        return
    for k, v in attrs.items():
        if v is not None:
            s.set_attribute(k, v)


class _NoopTracer:
    """Zero-cost stand-in when OTel is unavailable."""

    @contextmanager
    def start_as_current_span(self, name: str, **kwargs: Any) -> Any:
        yield _NoopSpan()


class _NoopSpan:
    """No-op span that safely ignores all calls."""

    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def set_status(self, *args: Any, **kwargs: Any) -> None:
        pass

    def record_exception(self, exc: BaseException) -> None:
        pass

    def add_event(self, name: str, attributes: dict | None = None) -> None:
        pass


def get_current_trace_context() -> dict[str, str]:
    """Return current trace_id and span_id as hex strings, or "-" if none."""
    if not _HAS_OTEL:
        return {"trace_id": "-", "span_id": "-"}
    current = trace.get_current_span()
    ctx = current.get_span_context()
    if ctx and ctx.trace_id != 0:
        return {
            "trace_id": format(ctx.trace_id, "032x"),
            "span_id": format(ctx.span_id, "016x"),
        }
    return {"trace_id": "-", "span_id": "-"}


def is_available() -> bool:
    return _HAS_OTEL


def is_enabled() -> bool:
    from app.config import settings

    return _HAS_OTEL and settings.OTEL_ENABLED
