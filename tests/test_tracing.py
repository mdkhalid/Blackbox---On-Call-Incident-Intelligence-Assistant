"""Tracing facade and instrumentation tests.

The SDK is an optional extra, so these tests exercise the no-op path and inject a
recording tracer instead of requiring a collector. What matters is the span contract:
names, attributes, exception capture and route templates.
"""
from __future__ import annotations

import asyncio
import builtins
import sys
import types
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from blackbox.api.middleware import TracingMiddleware
from blackbox.config.settings import AppConfig, Settings
from blackbox.graph.build import _traced_node
from blackbox.graph.nodes import GraphDeps, _acomplete, retrieve_node
from blackbox.llm.base import LLMResponse
from blackbox.main import create_app
from blackbox.observability.tracing import (
    NullTracer,
    OTelTracer,
    _build_sampler,
    _parse_headers,
    configure_tracing,
    current_trace_id,
    get_tracer,
    reset_tracer,
    set_tracer,
    shutdown_tracing,
    span,
)
from tests.test_api_chat import _make_services


@dataclass
class RecordedSpan:
    name: str
    attributes: dict[str, Any] = field(default_factory=dict)
    exceptions: list[BaseException] = field(default_factory=list)

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def set_name(self, name: str) -> None:
        self.name = name

    def record_exception(self, exception: BaseException) -> None:
        self.exceptions.append(exception)


class RecordingTracer:
    enabled = True

    def __init__(self) -> None:
        self.spans: list[RecordedSpan] = []

    @contextmanager
    def span(self, name: str, /, **attributes: Any) -> Iterator[RecordedSpan]:
        recorded = RecordedSpan(name, dict(attributes))
        self.spans.append(recorded)
        yield recorded

    def current_trace_id(self) -> str | None:
        return "0" * 32

    def named(self, name: str) -> list[RecordedSpan]:
        return [item for item in self.spans if item.name == name]


@pytest.fixture
def tracer() -> Iterator[RecordingTracer]:
    recording = RecordingTracer()
    set_tracer(recording)
    try:
        yield recording
    finally:
        reset_tracer()


def test_null_tracer_is_the_default_and_is_silent():
    reset_tracer()
    assert isinstance(get_tracer(), NullTracer)
    assert get_tracer().enabled is False
    with span("anything", attribute="value") as current:
        current.set_attribute("later", 1)
        current.set_name("renamed")
    assert current_trace_id() is None


def test_disabled_settings_install_no_tracer():
    assert configure_tracing(Settings(otel_enabled=False)) is False
    assert get_tracer().enabled is False


def test_missing_sdk_falls_back_to_noop(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("opentelemetry"):
            raise ImportError("optional dependency not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert configure_tracing(Settings(otel_enabled=True)) is False
    assert get_tracer().enabled is False


class _FakeSpanContext:
    is_valid = True
    trace_id = 0xABC


class _FakeSpan:
    def set_attribute(self, key, value):
        return None

    def set_name(self, name):
        return None

    def record_exception(self, exception):
        return None

    def get_span_context(self):
        return _FakeSpanContext()


def _register(monkeypatch, name: str, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, name, module)
    if "." in name:
        parent_name, child = name.rsplit(".", 1)
        parent = sys.modules.get(parent_name)
        if parent is not None:
            setattr(parent, child, module)
    return module


def _install_fake_otel(monkeypatch, *, exporter_error: Exception | None = None):
    class FakeTracer:
        def __init__(self):
            self.last: tuple[str, dict] | None = None

        def start_as_current_span(self, name, attributes=None):
            self.last = (name, attributes)
            return nullcontext(_FakeSpan())

    instances: list = []

    class FakeProvider:
        def __init__(self, resource=None, sampler=None):
            self.resource = resource
            self.sampler = sampler
            self.processors: list = []
            self.tracers: list[FakeTracer] = []
            self.shutdown_called = False
            instances.append(self)

        def add_span_processor(self, processor):
            self.processors.append(processor)

        def get_tracer(self, name):
            tracer = FakeTracer()
            self.tracers.append(tracer)
            return tracer

        def shutdown(self):
            self.shutdown_called = True

    class FakeExporter:
        def __init__(self, **kwargs):
            if exporter_error is not None:
                raise exporter_error
            self.kwargs = kwargs

    class FakeProcessor:
        def __init__(self, exporter):
            self.exporter = exporter

    _register(monkeypatch, "opentelemetry")
    trace_module = _register(
        monkeypatch,
        "opentelemetry.trace",
        get_current_span=lambda: _FakeSpan(),
        set_tracer_provider=lambda provider: None,
    )
    assert sys.modules["opentelemetry"].trace is trace_module
    _register(monkeypatch, "opentelemetry.exporter")
    _register(monkeypatch, "opentelemetry.exporter.otlp")
    _register(monkeypatch, "opentelemetry.exporter.otlp.proto")
    _register(monkeypatch, "opentelemetry.exporter.otlp.proto.http")
    _register(
        monkeypatch,
        "opentelemetry.exporter.otlp.proto.http.trace_exporter",
        OTLPSpanExporter=FakeExporter,
    )
    _register(monkeypatch, "opentelemetry.sdk")
    resources = _register(
        monkeypatch,
        "opentelemetry.sdk.resources",
        Resource=type("Resource", (), {"create": staticmethod(lambda attrs: dict(attrs))}),
    )
    assert sys.modules["opentelemetry.sdk"].resources is resources
    _register(monkeypatch, "opentelemetry.sdk.trace", TracerProvider=FakeProvider)
    _register(
        monkeypatch,
        "opentelemetry.sdk.trace.export",
        BatchSpanProcessor=FakeProcessor,
    )
    _register(
        monkeypatch,
        "opentelemetry.sdk.trace.sampling",
        ALWAYS_OFF="always_off",
        ALWAYS_ON="always_on",
        ParentBased=lambda sampler: ("parent", sampler),
        TraceIdRatioBased=lambda ratio: ("ratio", ratio),
    )
    return FakeProvider, instances


def test_configure_tracing_with_fake_sdk(monkeypatch):
    _, instances = _install_fake_otel(monkeypatch)
    settings = Settings(
        otel_enabled=True,
        otel_service_name="blackbox-test",
        otel_exporter_otlp_endpoint="http://collector:4318",
        otel_exporter_otlp_headers="authorization=Bearer token, x-scope=test",
        otel_traces_sampler="parentbased_traceidratio",
        otel_traces_sampler_arg=0.25,
    )

    assert configure_tracing(settings) is True
    assert get_tracer().enabled is True

    provider = instances[-1]
    assert provider.resource["service.name"] == "blackbox-test"
    assert provider.resource["deployment.environment"] == settings.env
    assert provider.sampler == ("parent", ("ratio", 0.25))
    exporter = provider.processors[0].exporter
    assert exporter.kwargs["endpoint"] == "http://collector:4318"
    assert exporter.kwargs["headers"] == {
        "authorization": "Bearer token",
        "x-scope": "test",
    }

    with span("demo", kept=1, dropped=None) as current:
        current.set_attribute("during", "span")
    fake_tracer = provider.tracers[-1]
    assert fake_tracer.last == ("demo", {"kept": 1})
    assert current_trace_id() == f"{0xABC:032x}"

    shutdown_tracing()
    assert provider.shutdown_called is True
    assert get_tracer().enabled is False


def test_invalid_exporter_config_falls_back_to_noop(monkeypatch):
    _install_fake_otel(monkeypatch, exporter_error=ValueError("bad endpoint"))

    assert configure_tracing(Settings(otel_enabled=True)) is False
    assert get_tracer().enabled is False


def test_sampler_and_headers_helpers():
    kwargs = {
        "always_on": "on",
        "always_off": "off",
        "parent_based": lambda sampler: ("parent", sampler),
        "ratio": lambda value: ("ratio", value),
    }
    assert _build_sampler("always_on", 0.1, **kwargs) == "on"
    assert _build_sampler("always_off", 0.1, **kwargs) == "off"
    assert _build_sampler("traceidratio", 0.5, **kwargs) == ("ratio", 0.5)
    assert _build_sampler("anything-else", 0.5, **kwargs) == (
        "parent",
        ("ratio", 0.5),
    )
    assert _parse_headers("a=1, b = two,c") == {"a": "1", "b": "two"}


def test_otel_tracer_delegates_attributes():
    class FakeTracer:
        def start_as_current_span(self, name, attributes=None):
            self.called = (name, attributes)
            return nullcontext(_FakeSpan())

    fake = FakeTracer()
    tracer = OTelTracer(fake, trace_module=types.SimpleNamespace())
    with tracer.span("name", keep=1, drop=None):
        pass
    assert fake.called == ("name", {"keep": 1})



def test_otel_settings_are_validated():
    with pytest.raises(ValidationError):
        Settings(otel_traces_sampler="not-a-sampler")
    with pytest.raises(ValidationError):
        Settings(otel_traces_sampler_arg=1.5)
    with pytest.raises(ValidationError):
        Settings(otel_exporter_timeout_s=0)
    parsed = Settings(otel_ignore_paths="/health, /metrics")
    assert parsed.otel_ignore_path_list == ["/health", "/metrics"]


async def test_llm_span_records_node_model_and_usage(tracer):
    class Provider:
        async def acomplete(self, **kwargs) -> LLMResponse:
            return LLMResponse(
                text="ok", model="fake", prompt_tokens=3, completion_tokens=4
            )

    response = await _acomplete(
        Provider(),
        node="generate",
        system="system",
        prompt="prompt",
        model="gpt-test",
        max_tokens=64,
    )

    assert response.total_tokens == 7
    recorded = tracer.named("llm.complete")
    assert len(recorded) == 1
    attributes = recorded[0].attributes
    assert attributes["llm.node"] == "generate"
    assert attributes["llm.model"] == "gpt-test"
    assert attributes["llm.max_tokens"] == 64
    assert attributes["llm.prompt_tokens"] == 3
    assert attributes["llm.completion_tokens"] == 4
    assert attributes["llm.total_tokens"] == 7
    assert attributes["llm.cached"] is False


async def test_llm_span_records_exceptions(tracer):
    class FailingProvider:
        async def acomplete(self, **kwargs) -> LLMResponse:
            raise RuntimeError("provider down")

    with pytest.raises(RuntimeError):
        await _acomplete(
            FailingProvider(),
            node="grade",
            system="system",
            prompt="prompt",
            model="gpt-test",
        )

    recorded = tracer.named("llm.complete")[0]
    assert recorded.attributes["llm.node"] == "grade"
    assert isinstance(recorded.exceptions[0], RuntimeError)


async def test_retrieval_span_records_chunks_and_timings(tracer):
    class Retriever:
        def retrieve(self, query, *, top_k, filter, tenant_id=None):
            return []

    deps = GraphDeps(
        retriever=Retriever(),
        llm=object(),
        config=AppConfig(),
        settings=Settings(),
    )
    await retrieve_node({"query": "pool exhaustion", "tenant_id": "t1"}, deps)

    recorded = tracer.named("retrieval.retrieve")
    assert len(recorded) == 1
    assert recorded[0].attributes["retrieval.chunks"] == 0
    assert recorded[0].attributes["retrieval.top_k"] == 8
    assert recorded[0].attributes["retrieval.tenant_id"] == "t1"


async def test_graph_node_span_records_outcome(tracer):
    async def handler(state):
        return {
            "status": "answered",
            "route": state.get("route"),
            "chunks": [{"chunk_id": "a"}],
            "grade": {"sufficient": True},
        }

    result = await _traced_node("generate", handler)({"route": "retrieval"})

    assert result["status"] == "answered"
    recorded = tracer.named("graph.node.generate")
    assert len(recorded) == 1
    assert recorded[0].attributes["graph.node"] == "generate"
    assert recorded[0].attributes["graph.route"] == "retrieval"
    assert recorded[0].attributes["graph.chunks"] == 1
    assert recorded[0].attributes["graph.status"] == "answered"
    assert recorded[0].attributes["graph.grade_sufficient"] is True


def test_http_span_uses_route_template(tracer, settings):
    client = TestClient(create_app(settings))

    response = client.get("/api/conversations/does-not-exist")

    assert response.status_code in {404, 503}
    recorded = tracer.named("GET /api/conversations/{conversation_id}")
    assert len(recorded) == 1
    assert recorded[0].attributes["http.route"] == "/api/conversations/{conversation_id}"
    assert "does-not-exist" not in recorded[0].name


def test_probe_paths_are_not_traced(tracer, settings):
    client = TestClient(create_app(settings))

    assert client.get("/health").status_code == 200

    assert tracer.named("GET /health") == []
    assert not any(item.name.startswith("http.request") for item in tracer.spans)


def test_chat_turn_emits_nested_spans(
    tracer, tmp_path, vectorstore, settings
):
    from blackbox.llm import FakeLLMProvider

    services = _make_services(tmp_path, vectorstore, settings, FakeLLMProvider())
    client = TestClient(create_app(settings, services=services))

    response = client.post(
        "/api/chat", json={"message": "What caused ERR_POOL_EXHAUSTED in checkout-service?"}
    )

    assert response.status_code == 200, response.text
    assert tracer.named("chat.turn")
    turn = tracer.named("chat.turn")[0]
    assert turn.attributes["chat.status"] == "answered"
    assert turn.attributes["tenant.id"] == "default"
    assert "conversation.id" in turn.attributes
    assert turn.attributes["chat.answer_chars"] > 0

    assert tracer.named("db.record_turn")
    assert tracer.named("db.record_turn")[0].attributes["db.status"] == "answered"
    assert tracer.named("graph.node.router")
    assert tracer.named("graph.node.generate")
    assert tracer.named("llm.complete")
    assert tracer.named("retrieval.retrieve")


def test_tracing_middleware_records_exceptions():
    """An exception escaping the app is attached to the request span, not swallowed."""

    recording = RecordingTracer()
    set_tracer(recording)

    async def failing_app(scope, receive, send):
        raise RuntimeError("boom")

    middleware = TracingMiddleware(failing_app)
    scope = {"type": "http", "method": "GET", "path": "/explode"}

    async def receive():
        return {"type": "http.request"}

    async def send(message):
        return None

    try:
        with pytest.raises(RuntimeError):
            asyncio.run(middleware(scope, receive, send))
    finally:
        reset_tracer()

    assert recording.spans[0].exceptions
    assert isinstance(recording.spans[0].exceptions[0], RuntimeError)
