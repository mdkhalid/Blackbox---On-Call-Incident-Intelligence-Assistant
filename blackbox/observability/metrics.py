"""Prometheus metrics with no runtime dependency.

The service needs a scrape endpoint that the Helm chart can wire to Prometheus and
that HPA/KEDA can scale on. The Prometheus text exposition format is small and
stable, so a local registry keeps the runtime dependency-free and the behaviour
testable offline — the same rule the rest of the project follows for retrieval and
generation.

Metric *names* are the contract: dashboards and alert rules refer to them by name,
so they are defined once here and every call site goes through a helper.
"""
from __future__ import annotations

import math
import re
import threading
from collections.abc import Iterable, Sequence
from typing import TypeVar

_NAME = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
_LABEL = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

DEFAULT_BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
)


def _format_value(value: float) -> str:
    if math.isfinite(value) and value == int(value):
        return str(int(value))
    return repr(value)


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_labels(labels: dict[str, str]) -> str:
    if not labels:
        return ""
    # Preserve insertion order: metric labels in definition order, then the histogram
    # `le` bucket, which is how Prometheus conventionally prints a histogram sample.
    pairs = ",".join(
        f'{key}="{_escape_label(str(value))}"' for key, value in labels.items()
    )
    return "{" + pairs + "}"


class Metric:
    """Common name/label validation and locking for one metric family."""

    kind = "untyped"

    def __init__(
        self, name: str, help_text: str, labelnames: Sequence[str] = ()
    ) -> None:
        if not _NAME.fullmatch(name):
            raise ValueError(f"invalid metric name: {name!r}")
        for label in labelnames:
            if not _LABEL.fullmatch(label):
                raise ValueError(f"invalid label name: {label!r}")
        self.name = name
        self.help_text = help_text
        self.labelnames = tuple(labelnames)
        self._lock = threading.Lock()

    def _key(self, labels: dict[str, str]) -> tuple[str, ...]:
        if set(labels) != set(self.labelnames):
            raise ValueError(
                f"{self.name} expects labels {self.labelnames}, got {sorted(labels)}"
            )
        return tuple(str(labels[name]) for name in self.labelnames)

    def _labelled(self, key: tuple[str, ...], **extra: str) -> dict[str, str]:
        labels = dict(zip(self.labelnames, key, strict=True))
        labels.update(extra)
        return labels

    def _render(self) -> Iterable[str]:  # pragma: no cover - overridden
        return ()


class Counter(Metric):
    kind = "counter"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._values: dict[tuple[str, ...], float] = {}

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        key = self._key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def value(self, **labels: str) -> float:
        with self._lock:
            return self._values.get(self._key(labels), 0.0)

    def _render(self) -> Iterable[str]:
        with self._lock:
            series = list(self._values.items())
        if not series and not self.labelnames:
            series = [((), 0.0)]
        for key, value in series:
            yield f"{self.name}{_format_labels(self._labelled(key))} {_format_value(value)}"


class Gauge(Metric):
    kind = "gauge"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._values: dict[tuple[str, ...], float] = {}

    def set(self, value: float, **labels: str) -> None:
        key = self._key(labels)
        with self._lock:
            self._values[key] = value

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        key = self._key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def dec(self, amount: float = 1.0, **labels: str) -> None:
        self.inc(-amount, **labels)

    def value(self, **labels: str) -> float:
        with self._lock:
            return self._values.get(self._key(labels), 0.0)

    def _render(self) -> Iterable[str]:
        with self._lock:
            series = list(self._values.items())
        if not series and not self.labelnames:
            series = [((), 0.0)]
        for key, value in series:
            yield f"{self.name}{_format_labels(self._labelled(key))} {_format_value(value)}"


class Histogram(Metric):
    kind = "histogram"

    def __init__(
        self,
        name: str,
        help_text: str,
        labelnames: Sequence[str] = (),
        *,
        buckets: Sequence[float] | None = None,
    ) -> None:
        super().__init__(name, help_text, labelnames)
        self.buckets = tuple(buckets or DEFAULT_BUCKETS)
        self._counts: dict[tuple[str, ...], list[int]] = {}
        self._sums: dict[tuple[str, ...], float] = {}
        self._totals: dict[tuple[str, ...], int] = {}

    def observe(self, value: float, **labels: str) -> None:
        key = self._key(labels)
        if value < 0:
            # Prometheus histogram samples are non-negative; a negative duration is a
            # clock bug, not a measurement worth exporting.
            value = 0.0
        with self._lock:
            counts = self._counts.setdefault(key, [0] * len(self.buckets))
            for index, bound in enumerate(self.buckets):
                if value <= bound:
                    counts[index] += 1
            self._sums[key] = self._sums.get(key, 0.0) + value
            self._totals[key] = self._totals.get(key, 0) + 1

    def count(self, **labels: str) -> int:
        with self._lock:
            return self._totals.get(self._key(labels), 0)

    def _render(self) -> Iterable[str]:
        with self._lock:
            keys = list(self._counts.items())
        if not keys and not self.labelnames:
            keys = [((), [0] * len(self.buckets))]
        for key, counts in keys:
            for bound, observed in zip(self.buckets, counts, strict=True):
                yield (
                    f"{self.name}_bucket"
                    f"{_format_labels(self._labelled(key, le=_format_value(bound)))}"
                    f" {observed}"
                )
            total = self._totals.get(key, 0)
            yield (
                f"{self.name}_bucket"
                f"{_format_labels(self._labelled(key, le='+Inf'))} {total}"
            )
            yield f"{self.name}_sum{_format_labels(self._labelled(key))} " \
                  f"{_format_value(self._sums.get(key, 0.0))}"
            yield f"{self.name}_count{_format_labels(self._labelled(key))} {total}"


_MetricT = TypeVar("_MetricT", bound=Metric)


class Registry:
    """A named collection of metric families rendered in one scrape."""
    def __init__(self) -> None:
        self._metrics: dict[str, Metric] = {}
        self._lock = threading.Lock()

    def register(self, metric: _MetricT) -> _MetricT:
        with self._lock:
            if metric.name in self._metrics:
                raise ValueError(f"metric already registered: {metric.name}")
            self._metrics[metric.name] = metric
        return metric

    def render(self) -> str:
        with self._lock:
            metrics = sorted(self._metrics.values(), key=lambda item: item.name)
        lines: list[str] = []
        for metric in metrics:
            lines.append(f"# HELP {metric.name} {metric.help_text}")
            lines.append(f"# TYPE {metric.name} {metric.kind}")
            lines.extend(metric._render())
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        """Clear samples (tests only; names stay registered)."""
        with self._lock:
            for metric in self._metrics.values():
                for attribute in ("_values", "_counts", "_sums", "_totals"):
                    collection = getattr(metric, attribute, None)
                    if isinstance(collection, dict):
                        collection.clear()


REGISTRY = Registry()


def _counter(name: str, help_text: str, labels: Sequence[str] = ()) -> Counter:
    return REGISTRY.register(Counter(name, help_text, labels))


def _gauge(name: str, help_text: str, labels: Sequence[str] = ()) -> Gauge:
    return REGISTRY.register(Gauge(name, help_text, labels))


def _histogram(
    name: str, help_text: str, labels: Sequence[str] = ()
) -> Histogram:
    return REGISTRY.register(Histogram(name, help_text, labels))


# ---- definitions (the contract with dashboards and alerts) ----

BUILD_INFO = _gauge(
    "blackbox_build_info",
    "Build and environment identity; value is always 1.",
    ("version", "env"),
)
HTTP_REQUESTS = _counter(
    "blackbox_http_requests_total",
    "HTTP requests by method, route template and status.",
    ("method", "path", "status"),
)
HTTP_REQUEST_DURATION = _histogram(
    "blackbox_http_request_duration_seconds",
    "HTTP request duration by method and route template.",
    ("method", "path"),
)
SSE_ACTIVE = _gauge(
    "blackbox_sse_active_streams",
    "Chat graph streams currently in flight on this pod.",
)
RETRIEVAL_DURATION = _histogram(
    "blackbox_retrieval_duration_seconds",
    "Retrieval stage latency (dense, sparse, fuse, rerank, total, cache).",
    ("stage",),
)
RETRIEVAL_CACHE_EVENTS = _counter(
    "blackbox_retrieval_cache_events_total",
    "Retrieval cache lookups by result.",
    ("result",),
)
LLM_CALLS = _counter(
    "blackbox_llm_calls_total",
    "LLM calls by graph node, model and cache result.",
    ("node", "model", "cached"),
)
LLM_DURATION = _histogram(
    "blackbox_llm_call_duration_seconds",
    "LLM call latency by graph node.",
    ("node",),
)
LLM_TOKENS = _counter(
    "blackbox_llm_tokens_total",
    "LLM tokens by graph node and kind (prompt, completion).",
    ("node", "kind"),
)
LLM_ERRORS = _counter(
    "blackbox_llm_errors_total",
    "LLM failures by graph node after retries are exhausted.",
    ("node",),
)
JOB_QUEUE_DEPTH = _gauge(
    "blackbox_job_queue_depth",
    "Pending background jobs by kind.",
    ("kind",),
)
JOB_QUEUE_LOSS_RISK = _gauge(
    "blackbox_job_queue_loss_risk",
    "Queued jobs that this region's Redis loss would strand.",
    ("kind",),
)
CACHE_FALLBACK_ACTIVE = _gauge(
    "blackbox_cache_fallback_active",
    "1 when a Redis-backed feature is running on its local fallback.",
)

_STAGE_NAMES = {
    "dense_ms": "dense",
    "sparse_ms": "sparse",
    "fuse_ms": "fuse",
    "rerank_ms": "rerank",
    "cache_ms": "cache",
    "total_ms": "total",
}


def set_build_info(version: str, env: str) -> None:
    BUILD_INFO.set(1, version=version, env=env)


def record_http(
    method: str, path: str, status: int, duration_s: float
) -> None:
    HTTP_REQUESTS.inc(method=method, path=path, status=str(status))
    HTTP_REQUEST_DURATION.observe(duration_s, method=method, path=path)


def record_retrieval(timings: dict[str, float]) -> None:
    """Observe the per-stage timings the retriever already reports."""
    hit = "cache_ms" in timings
    RETRIEVAL_CACHE_EVENTS.inc(result="hit" if hit else "miss")
    for key, value in timings.items():
        stage = _STAGE_NAMES.get(key)
        if stage is None:
            continue
        RETRIEVAL_DURATION.observe(float(value) / 1000.0, stage=stage)


def record_llm_call(
    *,
    node: str,
    model: str,
    cached: bool,
    prompt_tokens: int,
    completion_tokens: int,
    duration_ms: float,
) -> None:
    LLM_CALLS.inc(node=node, model=model or "unknown", cached="true" if cached else "false")
    LLM_DURATION.observe(max(0.0, duration_ms) / 1000.0, node=node)
    if prompt_tokens:
        LLM_TOKENS.inc(amount=float(prompt_tokens), node=node, kind="prompt")
    if completion_tokens:
        LLM_TOKENS.inc(amount=float(completion_tokens), node=node, kind="completion")


def record_llm_error(node: str) -> None:
    LLM_ERRORS.inc(node=node)


def record_job_queue_depth(kind: str, depth: int) -> None:
    JOB_QUEUE_DEPTH.set(float(max(0, depth)), kind=kind)
    JOB_QUEUE_LOSS_RISK.set(float(max(0, depth)), kind=kind)


def record_cache_fallback(active: bool) -> None:
    CACHE_FALLBACK_ACTIVE.set(1.0 if active else 0.0)


def render_metrics() -> str:
    return REGISTRY.render()


def reset_metrics() -> None:
    """Clear samples for test isolation."""
    REGISTRY.reset()
