"""The background job queue.

Ingesting a corpus and running the evaluation grid must not occupy a request worker
for the duration of the job, so both endpoints answer `202 {job_id}` and the work
happens on a background worker.
"""
import threading
import time

from blackbox.api.jobs import FAILED, SUCCEEDED, JobQueue


def _wait(job, *, timeout_s: float = 10.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if job.done:
            return job
        time.sleep(0.01)
    raise AssertionError(f"job did not finish: {job.status}")


def test_a_job_runs_and_reports_its_result():
    queue = JobQueue("test")
    job = queue.submit(lambda: {"documents": 60}, tenant_id="t", subject="s")

    finished = _wait(job)
    assert finished.status == SUCCEEDED
    assert finished.result == {"documents": 60}
    assert finished.error is None
    assert finished.started_at and finished.finished_at


def test_a_failing_job_is_reported_without_killing_the_worker():
    queue = JobQueue("test")

    def boom():
        raise RuntimeError("the corpus is corrupt")

    failed = queue.submit(boom, tenant_id="t", subject="s")
    finished = _wait(failed)
    assert finished.status == FAILED
    assert "RuntimeError" in finished.error
    assert "the corpus is corrupt" in finished.error

    # The worker must still be serving jobs.
    following = queue.submit(lambda: {"ok": True}, tenant_id="t", subject="s")
    assert _wait(following).status == SUCCEEDED


def test_jobs_of_one_kind_run_in_order():
    queue = JobQueue("test")
    order: list[str] = []
    gate = threading.Event()

    def blocking():
        order.append("first")
        gate.wait(timeout=5)
        return {"ok": True}

    first = queue.submit(blocking, tenant_id="t", subject="s")
    second = queue.submit(lambda: order.append("second"), tenant_id="t", subject="s")

    # The second job must not start before the first has finished.
    time.sleep(0.1)
    assert order == ["first"]
    gate.set()
    _wait(second)
    assert order == ["first", "second"]
    assert first.done


def test_a_job_is_found_by_id():
    queue = JobQueue("test")
    job = queue.submit(lambda: {"ok": True}, tenant_id="t", subject="s")
    assert queue.get(job.id) is job
    assert queue.get("nope") is None


def test_an_async_job_body_is_supported():
    queue = JobQueue("test")

    async def body():
        return {"ok": True}

    job = queue.submit(body, tenant_id="t", subject="s")
    finished = _wait(job)
    assert finished.status == SUCCEEDED
    assert finished.result == {"ok": True}
