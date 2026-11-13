"""Prometheus exposition tests: the scrape format is the contract."""
import pytest
from fastapi.testclient import TestClient

from blackbox.api.jobs import InProcessJobStore, Job
from blackbox.main import create_app
from blackbox.observability.metrics import (
    Counter,
    Gauge,
    Histogram,
    Registry,
    record_job_queue_depth,
    record_llm_call,
    record_retrieval,
    reset_metrics,
)


@pytest.fixture(autouse=True)
def clean_metrics():
    reset_metrics()
    yield
    reset_metrics()


def test_counter_gauge_and_histogram_render_prometheus_text():
    registry = Registry()
    counter = registry.register(Counter("test_events_total", "events", ("kind",)))
    gauge = registry.register(Gauge("test_in_flight", "in flight"))
    histogram = registry.register(
        Histogram("test_latency_seconds", "latency", ("stage",), buckets=(0.1, 1.0))
    )

    counter.inc(kind="ok")
    counter.inc(amount=2, kind="ok")
    gauge.set(3)
    histogram.observe(0.05, stage="retrieve")
    histogram.observe(0.5, stage="retrieve")
    histogram.observe(2.0, stage="retrieve")

    text = registry.render()
    assert "# TYPE test_events_total counter" in text
    assert 'test_events_total{kind="ok"} 3' in text
    assert "# TYPE test_in_flight gauge" in text
    assert "test_in_flight 3" in text
    # Bucket counts are cumulative, then +Inf, _sum and _count.
    assert 'test_latency_seconds_bucket{stage="retrieve",le="0.1"} 1' in text
    assert 'test_latency_seconds_bucket{stage="retrieve",le="1"} 2' in text
    assert 'test_latency_seconds_bucket{stage="retrieve",le="+Inf"} 3' in text
    assert 'test_latency_seconds_sum{stage="retrieve"} 2.55' in text
    assert 'test_latency_seconds_count{stage="retrieve"} 3' in text


def test_labels_are_validated_and_names_cannot_collide():
    registry = Registry()
    counter = registry.register(Counter("test_events_total", "events", ("kind",)))
    with pytest.raises(ValueError):
        counter.inc(wrong="label")
    with pytest.raises(ValueError):
        registry.register(Counter("test_events_total", "duplicate"))
    with pytest.raises(ValueError):
        Counter("bad-name", "bad name")
    with pytest.raises(ValueError):
        Counter("test_good_total", "bad label", ("not-a-label",))


def test_label_values_are_escaped():
    registry = Registry()
    counter = registry.register(Counter("test_events_total", "events", ("query",)))
    counter.inc(query='say "hi"\nnow\\')
    text = registry.render()
    assert 'query="say \\"hi\\"\\nnow\\\\"' in text


def test_http_metrics_use_the_route_template_not_the_raw_path(client):
    client.get("/api/conversations/does-not-exist/trace")
    text = client.get("/metrics").text
    # A conversation id in a label would create a new time series per conversation.
    assert (
        'blackbox_http_requests_total{method="GET",'
        'path="/api/conversations/{conversation_id}/trace",status="404"} 1'
    ) in text
    assert "does-not-exist" not in text


def test_metrics_endpoint_is_exempt_from_rate_limiting(settings, monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxy_cidrs", "")
    app = create_app(settings)
    # Rate limit is 5/s in the test config; 20 scrapes must all succeed.
    with TestClient(app) as client:
        for _ in range(20):
            response = client.get("/metrics")
            assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")


def test_build_info_is_set_during_lifespan(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        text = client.get("/metrics").text
    assert 'blackbox_build_info{version="' in text
    assert 'env="dev"} 1' in text


def test_llm_and_retrieval_helpers_export_the_right_labels():
    record_llm_call(
        node="generate",
        model="gpt-4o-mini",
        cached=False,
        prompt_tokens=120,
        completion_tokens=30,
        duration_ms=250,
    )
    record_retrieval(
        {"dense_ms": 10.0, "sparse_ms": 5.0, "fuse_ms": 1.0, "total_ms": 16.0}
    )
    record_retrieval({"cache_ms": 0.5, "total_ms": 0.5})
    record_job_queue_depth("ingest", 4)

    from blackbox.observability.metrics import render_metrics

    text = render_metrics()
    assert (
        'blackbox_llm_calls_total{node="generate",model="gpt-4o-mini",cached="false"} 1'
        in text
    )
    assert (
        'blackbox_llm_tokens_total{node="generate",kind="prompt"} 120' in text
    )
    assert (
        'blackbox_retrieval_cache_events_total{result="hit"} 1' in text
        and 'blackbox_retrieval_cache_events_total{result="miss"} 1' in text
    )
    assert 'blackbox_job_queue_depth{kind="ingest"} 4' in text


def test_in_process_job_store_reports_pending_by_kind():
    store = InProcessJobStore()
    ingest = Job(id="a", kind="ingest")
    retention = Job(id="b", kind="retention")
    for job in (ingest, retention):
        store.save(job)
        store.enqueue(job.id)
    assert store.pending_count("ingest") == 1
    assert store.pending_count("retention") == 1

    store.dequeue(timeout=1)
    # The first dequeued item belongs to whichever kind was enqueued first; the
    # totals must add up to one remaining job either way.
    assert store.pending_count("ingest") + store.pending_count("retention") == 1
