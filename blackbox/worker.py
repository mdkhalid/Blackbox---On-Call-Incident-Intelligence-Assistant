"""Dedicated background-job worker.

The API can execute a job in-process, but a job queued while no API replica is
available - or one whose pod is about to be replaced - needs a consumer whose only
purpose is to run jobs. This entry point is that consumer:

    python -m blackbox.worker

It shares the job store with the API, so with ``REDIS_URL`` set, jobs submitted to any
replica are picked up here and their status is visible from every replica. Without
Redis it falls back to the in-process store, which makes this process a no-op; that is
the honest behaviour, because an in-process queue cannot be shared across processes.

The process is a plain signal-driven loop. The job queues each own a worker thread,
so the loop itself only waits and then stops the queues, which drains the job in
flight.
"""
from __future__ import annotations

import signal
import threading

from blackbox.api.jobs import JOB_KINDS

# Importing the operations routes registers the job handlers. The worker executes the
# same bodies the API would, so the two can never diverge in behaviour. The routes
# package `__init__` is intentionally empty, so the module has to be imported by name;
# importing the package alone registers nothing.
from blackbox.api.routes import operations  # noqa: F401
from blackbox.api.services import build_services
from blackbox.config.settings import get_settings
from blackbox.logging_setup import get_logger, setup_logging

logger = get_logger(__name__)


def main() -> None:  # pragma: no cover - process entry point
    settings = get_settings()
    setup_logging(settings)

    services = build_services(settings)
    services.ensure_storage()

    queues = [services.job_queue(kind) for kind in JOB_KINDS]
    for queue in queues:
        # A regional Redis loss can leave job records without their pending entries.
        # Re-queue that work before this consumer starts, so it is not stranded.
        try:
            queue.redrive()
        except Exception as exc:  # noqa: BLE001 - recovery is best effort
            logger.warning("job_redrive_failed", kind=queue.kind, error=str(exc))
        queue.start()

    stop = threading.Event()

    def _handle(signum, _frame) -> None:
        logger.info("worker_stopping", signal=signum)
        stop.set()

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)

    shared = bool(services.job_store and services.job_store.shared)
    logger.info("worker_ready", kinds=list(JOB_KINDS), shared=shared)
    if not shared:
        logger.warning(
            "worker_without_shared_store",
            detail="REDIS_URL is not reachable; this worker cannot see jobs from other pods",
        )

    stop.wait()
    services.close()
    logger.info("worker_stopped")


if __name__ == "__main__":  # pragma: no cover
    main()
