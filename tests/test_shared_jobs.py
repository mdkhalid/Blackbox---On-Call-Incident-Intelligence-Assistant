"""Shared job store: a job submitted on one replica can run on another."""
from __future__ import annotations

import threading
import time

import pytest

from blackbox.api.jobs import (
    FAILED,
    SUCCEEDED,
    JobQueue,
    RedisJobStore,
    register_handler,
)


class FakeRedis:
    """The handful of Redis commands the job store uses."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.lists: dict[str, list[str]] = {}
        self._lock = threading.Lock()

    def set(self, key: str, value: str) -> None:
        with self._lock:
            self.values[key] = value

    def get(self, key: str):
        with self._lock:
            return self.values.get(key)

    def hset(self, name: str, field: str, value: str) -> None:
        with self._lock:
            self.hashes.setdefault(name, {})[field] = value

    def hget(self, name: str, field: str):
        with self._lock:
            return self.hashes.get(name, {}).get(field)

    def rpush(self, name: str, value: str) -> None:
        with self._lock:
            self.lists.setdefault(name, []).append(value)

    def blpop(self, name: str, timeout: int = 0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                items = self.lists.get(name)
                if items:
                    return (name, items.pop(0))
            time.sleep(0.01)
        return None


def _wait_for(queue, job_id, *, timeout_s: float = 5.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        job = queue.get(job_id)
        if job is not None and job.done:
            return job
        time.sleep(0.01)
    raise AssertionError("job did not finish")


@pytest.fixture
def redis_store():
    client = FakeRedis()
    return client, RedisJobStore(client)


def test_a_job_submitted_on_one_replica_runs_on_another(redis_store):
    client, store = redis_store
    seen: list[int] = []

    def handler(services, params):
        seen.append(params["x"])
        return {"echo": params["x"]}

    register_handler("shared_demo", handler)

    producer = JobQueue("shared_demo", store, autostart=False)
    consumer = JobQueue("shared_demo", RedisJobStore(client))

    job = producer.submit_params({"x": 7}, tenant_id="t1", subject="s1")
    # The producer has not started a worker, so the job is still queued.
    assert producer.get(job.id).status == "queued"

    consumer.start()
    try:
        finished = _wait_for(consumer, job.id)
    finally:
        consumer.stop()
        consumer.join(2)

    assert finished.status == SUCCEEDED
    assert finished.result == {"echo": 7}
    assert seen == [7]
    # The producing replica can observe the result too.
    assert producer.snapshot(job)["status"] == SUCCEEDED


def test_job_state_survives_a_new_queue_instance(redis_store):
    client, store = redis_store
    register_handler("persist_demo", lambda services, params: {"ok": True})
    producer = JobQueue("persist_demo", store, autostart=False)
    job = producer.submit_params({}, tenant_id="t", subject="s")

    consumer = JobQueue("persist_demo", RedisJobStore(client))
    consumer.start()
    try:
        _wait_for(consumer, job.id)
    finally:
        consumer.stop()
        consumer.join(2)

    # A fresh process (a new queue over the same Redis) sees the finished job.
    fresh = JobQueue("persist_demo", RedisJobStore(client))
    restored = fresh.get(job.id)
    assert restored is not None
    assert restored.status == SUCCEEDED
    assert restored.result == {"ok": True}
    assert restored.params == {}


def test_a_kind_without_a_handler_cannot_be_submitted(redis_store):
    _client, store = redis_store
    queue = JobQueue("no_handler_here", store, autostart=False)
    with pytest.raises(RuntimeError, match="no handler"):
        queue.submit_params({}, tenant_id="t", subject="s")


def test_a_closure_job_still_runs_on_a_shared_store(redis_store):
    client, _store = redis_store
    queue = JobQueue("closure_demo", RedisJobStore(client))
    try:
        job = queue.submit(lambda: {"local": True}, tenant_id="t", subject="s")
        finished = _wait_for(queue, job.id)
    finally:
        queue.stop()
        queue.join(2)
    assert finished.status == SUCCEEDED
    assert finished.result == {"local": True}


def test_an_async_handler_runs_to_completion(redis_store):
    client, _store = redis_store

    async def handler(services, params):
        return {"async": params["value"]}

    register_handler("async_demo", handler)
    queue = JobQueue("async_demo", RedisJobStore(client))
    try:
        job = queue.submit_params({"value": 3}, tenant_id="t", subject="s")
        finished = _wait_for(queue, job.id)
    finally:
        queue.stop()
        queue.join(2)
    assert finished.status == SUCCEEDED
    assert finished.result == {"async": 3}


def test_a_failing_handler_is_reported_and_the_worker_survives(redis_store):
    client, _store = redis_store

    def handler(services, params):
        if params.get("boom"):
            raise RuntimeError("handler exploded")
        return {"ok": True}

    register_handler("failing_demo", handler)
    queue = JobQueue("failing_demo", RedisJobStore(client))
    try:
        bad = queue.submit_params({"boom": True}, tenant_id="t", subject="s")
        finished = _wait_for(queue, bad.id)
        assert finished.status == FAILED
        assert "handler exploded" in finished.error

        good = queue.submit_params({}, tenant_id="t", subject="s")
        assert _wait_for(queue, good.id).status == SUCCEEDED
    finally:
        queue.stop()
        queue.join(2)
