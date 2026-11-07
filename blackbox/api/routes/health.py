"""Health endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Request

from blackbox import __version__
from blackbox.config.settings import get_settings

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(request: Request):
    settings = getattr(request.app.state, "settings", None) or get_settings()
    return {"status": "ok", "version": __version__, "env": settings.env}


@router.get("/health/ready")
async def ready(request: Request):
    # Phase 0: config loaded is sufficient. DB/Redis checks added later.
    settings = getattr(request.app.state, "settings", None) or get_settings()
    _ = settings
    return {"status": "ok"}
