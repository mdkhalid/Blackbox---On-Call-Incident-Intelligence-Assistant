"""Optional distributed tracing, with a no-op default.

The service is useful without a collector, so tracing is an *optional* capability:
instrumentation calls always exist, but they resolve to a no-op tracer unless
``OTEL_ENABLED=true`` and the ``otel`` extra is installed. That keeps the dependency
graph of the default install small and makes every call site testable without an SDK.

The facade is deliberately narrow (spans, attributes, exceptions, trace ids) so the
rest of the code never imports OpenTelemetry directly. Tests inject a recording tracer
through :func:`set_tracer` and assert on the spans that production code emits.

Span names are the contract with Tempo/Jaeger dashboards. They are stable strings
(``http.request``, ``graph.node.generate``, ``llm.complete``, ``retrieval.retrieve``,
``db.record_turn``, ``chat.turn``) and are listed in ``deploy/observability/README.md``.
"""
from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from typing import TYPE_CHECKING, Any, Protocol

from blackbox.logging_setup import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from blackbox.config.settings import Settings

logger = get_logger(__name__)


class Span(Protocol):
    """The subset of an OpenTelemetry span the application uses."""

    def set_attribute(self, key: str, value: Any) -> None: ...

    def set_name(self, name: str) -> None: ...

    def record_exception(self, exception: BaseException) -> None: ...


class Tracer(Protocol):
    """A tracer that produces spans or no-ops."""

    @property
    def enabled(self) -> bool: ...

    def span(
        self, name: str, /, **attributes: Any
    ) -> AbstractContextManager[Span]: ...

    def current_trace_id(self) -> str | None: ...


class _NullSpan:
    def set_attribute(self, key: str, value: Any) -> None:
        return None

    def set_name(self, name: str) -> None:
        return None

    def record_exception(self, exception: BaseException) -> None:
        return None


_NULL_SPAN = _NullSpan()


class NullTracer:
    """Used when tracing is disabled or the optional SDK is not installed."""

    enabled = False

    def span(self, name: str, /, **attributes: Any) -> AbstractContextManager[Span]:
        return nullcontext(_NULL_SPAN)

    def current_trace_id(self) -> str | None:
        return None


class OTelTracer:
    """Adapter over an OpenTelemetry ``Tracer``.

    Only :func:`configure_tracing` constructs this, so the SDK imports stay inside the
    enabled path and the default install never touches them.
    """

    enabled = True

    def __init__(self, tracer: Any, *, trace_module: Any) -> None:
        self._tracer = tracer
        self._trace = trace_module

    def span(self, name: str, /, **attributes: Any) -> AbstractContextManager[Span]:
        # None is not a valid attribute value; dropping it is friendlier than making
        # every call site build its attribute dict conditionally.
        cleaned = {key: value for key, value in attributes.items() if value is not None}
        return self._tracer.start_as_current_span(name, attributes=cleaned)

    def current_trace_id(self) -> str | None:
        context = self._trace.get_current_span().get_span_context()
        if not context.is_valid:
            return None
        return format(context.trace_id, "032x")


_TRACER: Tracer = NullTracer()
_PROVIDER: Any | None = None


def get_tracer() -> Tracer:
    return _TRACER


def set_tracer(tracer: Tracer) -> None:
    """Install a tracer. Tests use this to capture spans without an SDK."""
    global _TRACER
    _TRACER = tracer


def reset_tracer() -> None:
    global _TRACER
    _TRACER = NullTracer()


def span(name: str, /, **attributes: Any) -> AbstractContextManager[Span]:
    """Start a span, or a no-op context when tracing is disabled."""
    return _TRACER.span(name, **attributes)


def current_trace_id() -> str | None:
    """The active trace id, for log correlation; ``None`` when not tracing."""
    return _TRACER.current_trace_id()


def configure_tracing(settings: Settings) -> bool:
    """Enable tracing from settings. Returns whether a real tracer is installed.

    A misconfigured or missing SDK never stops the service: observability is a
    side-channel, and failing to boot because a collector library is absent would make
    the deployment less available than the incident it is meant to observe.
    """
    global _PROVIDER

    if not settings.otel_enabled:
        reset_tracer()
        return False

    try:
        from opentelemetry import trace as trace_module
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import (
            ALWAYS_OFF,
            ALWAYS_ON,
            ParentBased,
            TraceIdRatioBased,
        )
    except ImportError as exc:  # pragma: no cover - depends on the optional extra
        logger.warning(
            "otel_sdk_unavailable",
            error=type(exc).__name__,
            hint="install blackbox[otel] or set OTEL_ENABLED=false",
        )
        reset_tracer()
        return False

    sampler = _build_sampler(
        settings.otel_traces_sampler,
        settings.otel_traces_sampler_arg,
        always_on=ALWAYS_ON,
        always_off=ALWAYS_OFF,
        parent_based=ParentBased,
        ratio=TraceIdRatioBased,
    )
    resource = Resource.create(
        {
            "service.name": settings.otel_service_name,
            "service.version": _service_version(),
            "deployment.environment": settings.env,
        }
    )
    provider = TracerProvider(resource=resource, sampler=sampler)
    try:
        exporter = OTLPSpanExporter(
            endpoint=settings.otel_exporter_otlp_endpoint or None,
            headers=_parse_headers(settings.otel_exporter_otlp_headers),
            timeout=settings.otel_exporter_timeout_s,
        )
    except Exception as exc:  # noqa: BLE001 - config errors must not stop serving
        logger.warning("otel_exporter_invalid", error=type(exc).__name__)
        reset_tracer()
        return False
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace_module.set_tracer_provider(provider)

    _PROVIDER = provider
    set_tracer(
        OTelTracer(
            provider.get_tracer("blackbox"),
            trace_module=trace_module,
        )
    )
    logger.info(
        "otel_tracing_enabled",
        service_name=settings.otel_service_name,
        endpoint=settings.otel_exporter_otlp_endpoint,
        sampler=settings.otel_traces_sampler,
        sampler_arg=settings.otel_traces_sampler_arg,
    )
    return True


def shutdown_tracing() -> None:
    """Flush and stop the exporter. Safe to call when tracing was never enabled."""
    global _PROVIDER
    provider, _PROVIDER = _PROVIDER, None
    if provider is not None:
        try:
            provider.shutdown()
        except Exception:  # pragma: no cover - shutdown must never break exit
            logger.warning("otel_shutdown_failed", exc_info=True)
    reset_tracer()


def _build_sampler(
    name: str,
    arg: float,
    *,
    always_on: Any,
    always_off: Any,
    parent_based: Any,
    ratio: Any,
) -> Any:
    if name == "always_on":
        return always_on
    if name == "always_off":
        return always_off
    if name == "traceidratio":
        return ratio(arg)
    # Parent-based is the default: a sampled upstream request keeps its spans, and a
    # root request is sampled at the configured ratio.
    return parent_based(ratio(arg))


def _parse_headers(raw: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for item in raw.split(","):
        key, separator, value = item.partition("=")
        if separator and key.strip():
            headers[key.strip()] = value.strip()
    return headers


def _service_version() -> str:
    from blackbox import __version__

    return __version__


__all__ = [
    "NullTracer",
    "OTelTracer",
    "Span",
    "Tracer",
    "configure_tracing",
    "current_trace_id",
    "get_tracer",
    "reset_tracer",
    "set_tracer",
    "shutdown_tracing",
    "span",
]
