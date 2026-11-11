"""Background job registry for the operational endpoints.

Seeding a corpus and running the evaluation grid are long jobs - the eval grid alone
runs every question against four strategies over the whole corpus. Running them
inside the request handler occupies a worker for the entire job and, because the
handler held a lock while it ran, made the API refuse the work rather than queue it.

The endpoints now answer `202 {job_id}` immediately and the job runs on a background
worker thread. The status endpoint reports the job's state and its result.

The worker is a thread rather than a task on the request's event loop, because a
request's loop is not guaranteed to outlive the request: the job would be cancelled
the moment the response was sent. A dedicated thread has the same lifetime as the
process.

The queue is in-process. A shared, durable queue (the Redis-backed worker described
in the scaling document) is the next step; the API shape here does not change when
that happens, only where the queue lives.
"""
from __future__ import annotations

import asyncio
import inspect
import queue
import threading
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from blackbox.logging_setup import get_logger

logger = get_logger(__name__)

QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"

TERMINAL_STATUSES = (SUCCEEDED, FAILED)


@dataclass
class Job:
    """One background job and everything a caller needs to observe it."""

    id: str
    kind: str
    status: str = QUEUED
    tenant_id: str = ""
    subject: str = ""
    submitted_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None

    @property
    def done(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class JobQueue:
    """A FIFO queue for one kind of job, served by a single worker thread.

    One worker per kind keeps the ordering predictable and keeps a second ingest from
    competing with the first for the same corpus and the same retriever.
    """

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self._jobs: dict[str, Job] = {}
        self._pending: queue.Queue[tuple[Job, Callable[[], Any]]] = queue.Queue()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def submit(self, fn: Callable[[], Any], *, tenant_id: str, subject: str) -> Job:
        job = Job(
            id=uuid.uuid4().hex,
            kind=self.kind,
            tenant_id=tenant_id,
            subject=subject,
            submitted_at=_now(),
        )
        with self._lock:
            self._jobs[job.id] = job
        self._pending.put((job, fn))
        self._ensure_worker()
        logger.info("job_submitted", job_id=job.id, kind=self.kind)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def snapshot(self, job: Job) -> dict[str, Any]:
        """A consistent copy of a job's state.

        The worker thread updates the job while a request thread may be reading it,
        so the copy is taken under the lock.
        """
        with self._lock:
            return job.to_dict()

    def stop(self) -> None:
        """Ask the worker to stop after the job in flight."""
        self._stop.set()

    def _ensure_worker(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=f"job-worker-{self.kind}",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                job, fn = self._pending.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._execute(job, fn)
            except BaseException:  # noqa: BLE001 - the worker must outlive one bad job
                _safe_log("job_worker_error", job_id=job.id, kind=self.kind)

    def _execute(self, job: Job, fn: Callable[[], Any]) -> None:
        with self._lock:
            job.status = RUNNING
            job.started_at = _now()
        _safe_log("job_started", job_id=job.id, kind=self.kind)
        try:
            result = _invoke(fn)
        except BaseException as exc:  # noqa: BLE001 - a failed job must not kill the worker
            with self._lock:
                # The error and the finish time are published *before* the terminal
                # status, so a reader that sees "failed" also sees why.
                job.error = f"{type(exc).__name__}: {exc}"
                job.finished_at = _now()
                job.status = FAILED
            _safe_log("job_failed", job_id=job.id, kind=self.kind)
            return
        with self._lock:
            job.result = result if isinstance(result, dict) else {"value": result}
            job.finished_at = _now()
            job.status = SUCCEEDED
        _safe_log("job_succeeded", job_id=job.id, kind=self.kind)


def _safe_log(event: str, **fields: Any) -> None:
    """Log without ever letting a logging failure kill the worker.

    During interpreter shutdown the log stream can already be closed, and a worker
    that dies on a logging error would lose the job's outcome.
    """
    try:
        logger.info(event, **fields)
    except Exception:  # noqa: BLE001 - logging must never break the worker
        # The log stream can already be closed during interpreter shutdown. There is
        # nowhere left to report this to, and the job's own state is already recorded.
        return


def _invoke(fn: Callable[[], Any]) -> Any:
    """Run a job body, whether it is synchronous or asynchronous.

    A synchronous body runs directly on the worker thread. An asynchronous body is run
    on an event loop of its own, which lets it await the graph directly rather than
    blocking the worker.
    """
    if inspect.iscoroutinefunction(fn):
        return asyncio.run(fn())
    return fn()


def _now() -> str:
    return datetime.now(UTC).isoformat()
