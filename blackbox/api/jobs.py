"""Background job registry for the operational endpoints.

Seeding a corpus and running the evaluation grid are long jobs - the eval grid alone
runs every question against four strategies over the whole corpus. Running them
inside the request handler occupies a worker for the entire job, and because the
handler held a lock while it ran, made the API refuse the work rather than queue it.

The endpoints answer `202 {job_id}` immediately and the job runs on a background
worker. The status endpoint reports the job's state and its result.

Two stores are supported:

* **in-process** (default): a thread and a FIFO queue in the pod's memory. Simple and
  correct for a single replica, but job state and pending work die with the pod.
* **Redis**: job records and the pending queue live in Redis, so any replica - or the
  dedicated worker process (``python -m blackbox.worker``) - can execute the job and
  any replica can report its status. The job body must be *described* rather than
  captured: the API submits ``kind + params`` and the executing process looks up the
  registered handler. A handler registry is therefore part of the contract; a job
  submitted from one process can be executed by another only if both have registered
  the same kind.

Closure-based jobs (``submit(fn, ...)``) remain supported for tests and for callers
that live in a single process. They are not serialisable, so on a shared store they
are executed only by the pod that submitted them and fail with a clear reason if
another pod picks them up first.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import queue
import threading
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from blackbox.logging_setup import get_logger

logger = get_logger(__name__)

QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"

TERMINAL_STATUSES = (SUCCEEDED, FAILED)

# The kinds the API can submit and the dedicated worker process serves.
JOB_KINDS = ("ingest", "eval", "retention")

_JOB_KEY = "blackbox:job:{job_id}"
_PENDING_KEY = "blackbox:jobs:pending:{kind}"
_JOB_INDEX = "blackbox:jobs:index"

JobHandler = Callable[[Any, dict[str, Any]], Any]
_HANDLERS: dict[str, JobHandler] = {}


def register_handler(kind: str, handler: JobHandler) -> None:
    """Make ``kind`` executable in this process (and in the worker process)."""
    _HANDLERS[kind] = handler


def handler_for(kind: str) -> JobHandler | None:
    return _HANDLERS.get(kind)


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
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def done(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Job:
        return cls(**{key: value for key, value in data.items() if key in cls.__dataclass_fields__})


class JobStore(Protocol):
    """Where a queue keeps job records and pending work."""

    shared: bool

    def save(self, job: Job) -> None: ...

    def load(self, job_id: str) -> Job | None: ...

    def load_any(self, job_id: str) -> Job | None: ...

    def enqueue(self, job_id: str, fn: Callable[[], Any] | None = None) -> None: ...

    def dequeue(self, timeout: float) -> tuple[str, Callable[[], Any] | None] | None: ...

    def close(self) -> None: ...


class InProcessJobStore:
    """Job state in this process only."""

    shared = False

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._pending: queue.Queue[tuple[str, Callable[[], Any] | None]] = queue.Queue()
        self._lock = threading.Lock()

    def save(self, job: Job) -> None:
        with self._lock:
            self._jobs[job.id] = job

    def load(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def load_any(self, job_id: str) -> Job | None:
        return self.load(job_id)

    def enqueue(self, job_id: str, fn: Callable[[], Any] | None = None) -> None:
        self._pending.put((job_id, fn))

    def dequeue(self, timeout: float) -> tuple[str, Callable[[], Any] | None] | None:
        try:
            return self._pending.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        return None


class RedisJobStore:
    """Job records and pending work in Redis, usable from any replica."""

    shared = True

    def __init__(self, client: Any) -> None:
        self.client = client

    def save(self, job: Job) -> None:
        self.client.set(_JOB_KEY.format(job_id=job.id), json.dumps(job.to_dict()))
        self.client.hset(_JOB_INDEX, job.id, job.kind)

    def load(self, job_id: str) -> Job | None:
        raw = self.client.get(_JOB_KEY.format(job_id=job_id))
        if not raw:
            return None
        try:
            return Job.from_dict(json.loads(raw))
        except (TypeError, ValueError):
            return None

    def _kind_of(self, job_id: str) -> str | None:
        kind = self.client.hget(_JOB_INDEX, job_id)
        if isinstance(kind, bytes):
            kind = kind.decode()
        return kind

    def load_any(self, job_id: str) -> Job | None:
        return self.load(job_id) if self._kind_of(job_id) else None

    def enqueue(self, job_id: str, fn: Callable[[], Any] | None = None) -> None:
        job = self.load(job_id)
        if job is None:
            return
        self.client.rpush(_PENDING_KEY.format(kind=job.kind), job_id)

    def dequeue(self, timeout: float) -> tuple[str, Callable[[], Any] | None] | None:
        # One queue per kind, so this only returns jobs of our kind. Each kind has
        # its own JobQueue and its own pending key.
        raise NotImplementedError

    def dequeue_kind(
        self, kind: str, timeout: float
    ) -> tuple[str, Callable[[], Any] | None] | None:
        item = self.client.blpop(_PENDING_KEY.format(kind=kind), timeout=max(1, int(timeout)))
        if not item:
            return None
        job_id = item[1]
        if isinstance(job_id, bytes):
            job_id = job_id.decode()
        return job_id, None

    def close(self) -> None:
        return None


class JobQueue:
    """A FIFO queue for one kind of job, served by a single worker thread.

    One worker per kind keeps the ordering predictable and keeps a second ingest from
    competing with the first for the same corpus and the same retriever.
    """

    def __init__(
        self,
        kind: str,
        store: JobStore | None = None,
        *,
        services: Any | None = None,
        autostart: bool = True,
    ) -> None:
        self.kind = kind
        self.store = store or InProcessJobStore()
        self.services = services
        self._lock = threading.Lock()
        self._local: dict[str, Callable[[], Any]] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._autostart = autostart

    # ---- public API ----

    def submit(self, fn: Callable[[], Any], *, tenant_id: str, subject: str) -> Job:
        """Submit a closure. Local-only on a shared store; see the module docstring."""
        job = Job(
            id=uuid.uuid4().hex,
            kind=self.kind,
            tenant_id=tenant_id,
            subject=subject,
            submitted_at=_now(),
        )
        self.store.save(job)
        if self.store.shared:
            # A closure cannot travel through Redis. Keep it in this process; if
            # another replica wins the dequeue, the job fails with a clear reason.
            self._local[job.id] = fn
        self.store.enqueue(job.id, fn)
        self._maybe_start()
        logger.info("job_submitted", job_id=job.id, kind=self.kind, shared=False)
        return job

    def submit_params(
        self,
        params: dict[str, Any],
        *,
        tenant_id: str,
        subject: str,
    ) -> Job:
        """Submit a serialisable job body executed by a registered handler."""
        if handler_for(self.kind) is None:
            raise RuntimeError(f"no handler registered for job kind: {self.kind}")
        job = Job(
            id=uuid.uuid4().hex,
            kind=self.kind,
            tenant_id=tenant_id,
            subject=subject,
            submitted_at=_now(),
            params=params,
        )
        self.store.save(job)
        self.store.enqueue(job.id)
        self._maybe_start()
        logger.info("job_submitted", job_id=job.id, kind=self.kind, shared=self.store.shared)
        return job

    def get(self, job_id: str) -> Job | None:
        return self.store.load(job_id)

    def snapshot(self, job: Job) -> dict[str, Any]:
        """A consistent copy of a job's state.

        The worker thread updates an in-process job while a request thread may be
        reading it, so the copy is taken under the lock. A shared store is read back
        instead, which also picks up work done by another replica.
        """
        if self.store.shared:
            fresh = self.store.load(job.id)
            if fresh is not None:
                return fresh.to_dict()
        with self._lock:
            return job.to_dict()

    def start(self) -> None:
        """Start the worker thread if it is not running."""
        self._ensure_worker()

    def stop(self) -> None:
        """Ask the worker to stop after the job in flight."""
        self._stop.set()

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def drain_once(self, timeout: float = 0.2) -> bool:
        """Execute at most one pending job. Returns whether one was executed."""
        item = self._dequeue(timeout)
        if item is None:
            return False
        job_id, fn = item
        job = self.store.load(job_id)
        if job is None:
            return False
        if fn is None:
            fn = self._local.pop(job_id, None)
        self._execute(job, fn)
        return True

    # ---- worker ----

    def _maybe_start(self) -> None:
        if self._autostart:
            self._ensure_worker()

    def _dequeue(self, timeout: float) -> tuple[str, Callable[[], Any] | None] | None:
        if isinstance(self.store, RedisJobStore):
            return self.store.dequeue_kind(self.kind, timeout)
        return self.store.dequeue(timeout)

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
                self.drain_once(timeout=0.2)
            except BaseException:  # noqa: BLE001 - the worker must outlive one bad job
                _safe_log("job_worker_error", kind=self.kind)

    def _execute(self, job: Job, fn: Callable[[], Any] | None) -> None:
        if fn is None:
            handler = handler_for(job.kind)
            if handler is None:
                self._fail(
                    job,
                    RuntimeError(
                        f"no handler registered for job kind: {job.kind}"
                    ),
                )
                return
            params = job.params
            services = self.services
            fn = lambda: handler(services, params)

        job.status = RUNNING
        job.started_at = _now()
        self.store.save(job)
        _safe_log("job_started", job_id=job.id, kind=self.kind)
        try:
            result = _invoke(fn)
        except BaseException as exc:  # noqa: BLE001 - a failed job must not kill the worker
            self._fail(job, exc)
            return
        job.result = result if isinstance(result, dict) else {"value": result}
        job.finished_at = _now()
        job.status = SUCCEEDED
        self.store.save(job)
        _safe_log("job_succeeded", job_id=job.id, kind=self.kind)

    def _fail(self, job: Job, exc: BaseException) -> None:
        with self._lock:
            # The error and the finish time are published *before* the terminal
            # status, so a reader that sees "failed" also sees why.
            job.error = f"{type(exc).__name__}: {exc}"
            job.finished_at = _now()
            job.status = FAILED
        self.store.save(job)
        _safe_log("job_failed", job_id=job.id, kind=self.kind)


def build_job_store(settings: Any) -> JobStore:
    """Redis when it is configured and reachable, in-process otherwise."""
    url = getattr(settings, "redis_url", None)
    if url:
        try:
            import redis

            client = redis.Redis.from_url(url, decode_responses=True)
            client.ping()
            logger.info("job_store_shared", backend="redis")
            return RedisJobStore(client)
        except Exception as exc:  # noqa: BLE001 - fall back to the pod's memory
            logger.warning("job_store_redis_unavailable", error=str(exc))
    return InProcessJobStore()


def _safe_log(event: str, **fields: Any) -> None:
    """Log without ever letting a logging failure kill the worker.

    During interpreter shutdown the log stream can already be closed, and a worker
    that dies on a logging error would lose the job's outcome.
    """
    try:
        logger.info(event, **fields)
    except Exception:  # noqa: BLE001 - logging must never break the worker
        return


def _invoke(fn: Callable[[], Any]) -> Any:
    """Run a job body, whether it is synchronous or asynchronous.

    A synchronous body runs directly on the worker thread. An asynchronous body is run
    on an event loop of its own, which lets it await the graph directly rather than
    blocking the worker.
    """
    if inspect.iscoroutinefunction(fn):
        return asyncio.run(fn())
    result = fn()
    if inspect.iscoroutine(result):
        return asyncio.run(result)
    return result


def _now() -> str:
    return datetime.now(UTC).isoformat()
