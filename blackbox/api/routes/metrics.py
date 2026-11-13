"""Prometheus scrape endpoint.

Kept separate from `/health` so liveness/readiness stays cheap and the scrape can
carry its own (larger) payload. The endpoint is exempt from the per-IP rate limit
and is expected to be reachable only from the monitoring namespace — the Helm
NetworkPolicy is what enforces that, not authentication, because Prometheus cannot
present a user JWT.
"""
from __future__ import annotations

from fastapi import APIRouter, Request, Response

from blackbox.logging_setup import get_logger
from blackbox.observability.metrics import record_job_queue_depth, render_metrics

logger = get_logger(__name__)
router = APIRouter(tags=["observability"])

_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> Response:
    services = getattr(request.app.state, "services", None)
    if services is not None:
        # Queue depth is read at scrape time rather than maintained as a live gauge:
        # a queue can also be drained by another replica, so a cached number would be
        # stale in exactly the case the scaler cares about. A scrape must never fail
        # because one store call did.
        for kind, queue in list(services.job_queues.items()):
            try:
                record_job_queue_depth(kind, queue.pending_count())
            except Exception as exc:  # noqa: BLE001 - metrics are best-effort
                logger.debug("job_queue_depth_failed", kind=kind, error=str(exc))
    return Response(content=render_metrics(), media_type=_CONTENT_TYPE)
