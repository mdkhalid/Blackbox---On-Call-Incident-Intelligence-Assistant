"""Health endpoints."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from blackbox import __version__
from blackbox.api.services import PersistenceUnavailable
from blackbox.config.settings import get_settings

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(request: Request):
    settings = getattr(request.app.state, "settings", None) or get_settings()
    return {"status": "ok", "version": __version__, "env": settings.env}


@router.get("/health/ready")
async def ready(request: Request):
    """Liveness of the process, readiness of the conversation store."""
    settings = getattr(request.app.state, "settings", None) or get_settings()
    services = getattr(request.app.state, "services", None)
    if services is None:
        return {"status": "ok", "env": settings.env, "storage": "not_initialized"}

    try:
        await asyncio.wait_for(run_in_threadpool(services.ensure_storage), timeout=2.0)
    except (TimeoutError, PersistenceUnavailable) as exc:
        raise HTTPException(status_code=503, detail="storage unavailable") from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="storage unavailable") from exc

    if getattr(request.app.state, "warmup_attempted", False) and not getattr(
        services, "runtime_ready", False
    ):
        detail = getattr(request.app.state, "warmup_error", None) or "runtime unavailable"
        raise HTTPException(status_code=503, detail=detail)

    return {
        "status": "ok",
        "env": settings.env,
        "storage": services.storage_backend or "unknown",
        "runtime": "ready" if getattr(services, "runtime_ready", False) else "cold",
        "cache": type(services.cache_backend).__name__ if services.cache_backend else "off",
    }
