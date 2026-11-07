"""FastAPI application factory."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from blackbox import __version__
from blackbox.api.middleware import LoggingMiddleware, RequestIdMiddleware
from blackbox.api.routes.health import router as health_router
from blackbox.config.settings import Settings, get_settings
from blackbox.logging_setup import setup_logging


def create_app(settings: Settings | None = None):
    if settings is None:
        settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        setup_logging(settings)
        yield

    app = FastAPI(title="Blackbox", version=__version__, lifespan=lifespan)
    app.add_middleware(LoggingMiddleware)
    app.add_middleware(RequestIdMiddleware)
    app.include_router(health_router)
    return app


app = create_app()
