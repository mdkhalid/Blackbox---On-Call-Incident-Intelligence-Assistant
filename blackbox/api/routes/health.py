"""Health endpoints."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from blackbox import __version__
from blackbox.api.services import PersistenceUnavailable
from blackbox.config.settings import get_settings
from blackbox.logging_setup import get_logger
from blackbox.observability.metrics import record_dependency_states

router = APIRouter(tags=["health"])
logger = get_logger(__name__)

_STORAGE_TIMEOUT_S = 2.0
_WARMUP_TIMEOUT_S = 10.0


@router.get("/health")
async def health(request: Request):
    settings = getattr(request.app.state, "settings", None) or get_settings()
    return {"status": "ok", "version": __version__, "env": settings.env}


@router.get("/health/ready")
async def ready(request: Request):
    """Readiness with per-dependency state.

    Three things a single boolean could not do:

    * the storage check is a real query, so a lost connection fails readiness;
    * warm-up is retried on every probe while the pod is cold, so a transient failure
      at boot does not leave it NotReady until a restart;
    * every dependency is reported separately, so an operator can see whether the
      database, the runtime, the cache or a retrieval store is the problem.
    """
    settings = getattr(request.app.state, "settings", None) or get_settings()
    services = getattr(request.app.state, "services", None)
    if services is None:
        return {"status": "ok", "env": settings.env, "storage": "not_initialized"}

    try:
        await asyncio.wait_for(
            run_in_threadpool(services.ensure_storage), timeout=_STORAGE_TIMEOUT_S
        )
    except (TimeoutError, PersistenceUnavailable):
        record_dependency_states({"storage": "down", "runtime": "cold"})
        return JSONResponse(
            status_code=503,
            content={
                "status": "not_ready",
                "env": settings.env,
                "storage": "down",
                "dependencies": {"storage": "down", "runtime": "cold"},
            },
        )
    except Exception:  # noqa: BLE001 - any storage failure is a readiness failure
        record_dependency_states({"storage": "down", "runtime": "cold"})
        return JSONResponse(
            status_code=503,
            content={
                "status": "not_ready",
                "env": settings.env,
                "storage": "down",
                "dependencies": {"storage": "down", "runtime": "cold"},
            },
        )

    if not getattr(services, "runtime_ready", False):
        await _retry_warm_up(request, services)

    report = await run_in_threadpool(services.dependency_report)
    record_dependency_states(report)
    is_ready = report["storage"] == "ok" and report["runtime"] == "ready"
    body = {
        "status": "ok" if is_ready else "degraded",
        "env": settings.env,
        "storage": services.storage_backend or report["storage"],
        "runtime": report["runtime"],
        "cache": report["cache"],
        "dependencies": report,
    }
    if not is_ready:
        return JSONResponse(status_code=503, content=body)
    return body


async def _retry_warm_up(request: Request, services) -> None:
    """Retry runtime warm-up while cold; record why it failed if it still does."""
    try:
        await asyncio.wait_for(
            run_in_threadpool(services.ensure_runtime), timeout=_WARMUP_TIMEOUT_S
        )
        request.app.state.warmup_attempted = True
        request.app.state.warmup_error = None
    except Exception as exc:  # noqa: BLE001 - readiness reports the failure
        request.app.state.warmup_attempted = True
        request.app.state.warmup_error = type(exc).__name__
        logger.warning("readiness_warmup_failed", error=str(exc))
