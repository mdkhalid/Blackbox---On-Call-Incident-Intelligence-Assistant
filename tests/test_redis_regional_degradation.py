"""Regional Redis is non-authoritative: degraded paths and job recovery."""
from __future__ import annotations

import threading

from blackbox.api.jobs import QUEUED, Job, RedisJobStore
from blackbox.cache.backend import TTLCache, build_cache
from blackbox.config.settings import AppConfig
from blackbox.observability.metrics import render_metrics


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.lists: dict[str, list[str]] = {}
        self._lock = threading.Lock()

    def set(self, key, value):
        self.values[key] = value

    def get(self, key):
        return self.values.get(key)

    def hset(self, name, field, value):
        self.hashes.setdefault(name, {})[field] = value

    def hget(self, name, field):
        return self.hashes.get(name, {}).get(field)

    def hgetall(self, name):
        return dict(self.hashes.get(name, {}))

    def rpush(self, name, value):
        self.lists.setdefault(name, []).append(value)

    def lrange(self, name, start, end):
        items = self.lists.get(name, [])
        return items if end == -1 else items[start : end + 1]

    def llen(self, name):
        return len(self.lists.get(name, []))


def test_redrive_requeues_stranded_jobs():
    client = FakeRedis()
    store = RedisJobStore(client)
    job = Job(id="job-1", kind="ingest", tenant_id="t", subject="s")
    store.save(job)  # writes the record and the index entry
    # Simulate a Redis restart that kept no pending list.
    assert client.lrange("blackbox:jobs:pending:ingest", 0, -1) == []

    assert store.redrive("ingest") == 1
    assert client.lrange("blackbox:jobs:pending:ingest", 0, -1) == ["job-1"]
    # Idempotent: the job is now pending, so a second redrive queues nothing.
    assert store.redrive("ingest") == 0


def test_redrive_ignores_finished_jobs():
    client = FakeRedis()
    store = RedisJobStore(client)
    job = Job(id="job-2", kind="eval", tenant_id="t", subject="s", status="succeeded")
    store.save(job)

    assert store.redrive("eval") == 0


def test_redrive_is_scoped_to_one_kind():
    client = FakeRedis()
    store = RedisJobStore(client)
    store.save(Job(id="a", kind="ingest", tenant_id="t", subject="s"))
    store.save(Job(id="b", kind="eval", tenant_id="t", subject="s"))

    assert store.redrive("ingest") == 1
    assert client.lrange("blackbox:jobs:pending:eval", 0, -1) == []


def test_in_process_store_redrive_is_a_noop():
    from blackbox.api.jobs import InProcessJobStore

    assert InProcessJobStore().redrive("ingest") == 0


def test_cache_falls_back_to_memory_without_redis(monkeypatch):
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv("REDIS_URL", "")
    from blackbox.config.settings import get_settings

    get_settings.cache_clear()
    settings = get_settings()
    cache = build_cache(settings, AppConfig())
    assert isinstance(cache, TTLCache)
    assert "blackbox_cache_fallback_active 1" in render_metrics()


def test_job_queue_loss_risk_metric_is_exported():
    from blackbox.observability.metrics import record_job_queue_depth

    record_job_queue_depth("ingest", 3)
    assert "blackbox_job_queue_loss_risk{kind=\"ingest\"} 3" in render_metrics()


def test_queued_status_constant_is_stable():
    # `redrive` only re-queues work in this state.
    assert QUEUED == "queued"
