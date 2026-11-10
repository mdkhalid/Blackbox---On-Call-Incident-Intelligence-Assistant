"""FastAPI application factory."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from blackbox import __version__
from blackbox.api.middleware import LoggingMiddleware, RequestIdMiddleware
from blackbox.api.routes.chat import router as chat_router
from blackbox.api.routes.conversations import router as conversations_router
from blackbox.api.routes.health import router as health_router
from blackbox.api.routes.operations import router as operations_router
from blackbox.api.services import AppServices, build_services
from blackbox.config.settings import Settings, get_settings
from blackbox.logging_setup import setup_logging


def create_app(settings: Settings | None = None, *, services: AppServices | None = None):
    if settings is None:
        settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.services = services or build_services(settings)
        setup_logging(settings)
        yield

    app = FastAPI(title="Blackbox", version=__version__, lifespan=lifespan)
    # Available before lifespan runs too (TestClient without a context manager, ASGI
    # servers that import the app without triggering startup).
    app.state.settings = settings
    app.state.services = services or build_services(settings)
    app.add_middleware(LoggingMiddleware)
    app.add_middleware(RequestIdMiddleware)
    app.include_router(health_router)
    app.include_router(chat_router)
    app.include_router(conversations_router)
    app.include_router(operations_router)
    return app


app = create_app()
