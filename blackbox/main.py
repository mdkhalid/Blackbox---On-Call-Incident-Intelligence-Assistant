"""FastAPI application factory."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from blackbox import __version__
from blackbox.api.middleware import (
    BodyLimitMiddleware,
    LoggingMiddleware,
    RateLimitMiddleware,
    RequestIdMiddleware,
)
from blackbox.api.routes.chat import router as chat_router
from blackbox.api.routes.conversations import router as conversations_router
from blackbox.api.routes.health import router as health_router
from blackbox.api.routes.metrics import router as metrics_router
from blackbox.api.routes.operations import router as operations_router
from blackbox.api.routes.ui import router as ui_router
from blackbox.api.services import AppServices, build_services
from blackbox.config.settings import Settings, get_settings
from blackbox.logging_setup import setup_logging
from blackbox.observability.metrics import set_build_info


def create_app(settings: Settings | None = None, *, services: AppServices | None = None):
    if settings is None:
        settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.services = services or build_services(settings)
        setup_logging(settings)
        set_build_info(__version__, settings.env)
        # LangGraph offloads any synchronous node to the event loop's default
        # executor. The default is `min(32, cpu_count + 4)`, which ties the request
        # path's concurrency to the machine size; bounding it explicitly makes the
        # ceiling a configured number rather than an accident of the host.
        loop = asyncio.get_running_loop()
        executor = ThreadPoolExecutor(
            max_workers=settings.max_worker_threads,
            thread_name_prefix="blackbox-worker",
        )
        loop.set_default_executor(executor)
        try:
            yield
        finally:
            app.state.services.close()
            executor.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(title="Blackbox", version=__version__, lifespan=lifespan)
    # Available before lifespan runs too (TestClient without a context manager, ASGI
    # servers that import the app without triggering startup).
    app.state.settings = settings
    app.state.services = services or build_services(settings)
    app.add_middleware(
        RateLimitMiddleware,
        services=app.state.services,
    )
    app.add_middleware(
        BodyLimitMiddleware,
        max_bytes=app.state.services.config.guardrails.max_request_bytes,
    )
    app.add_middleware(LoggingMiddleware)
    app.add_middleware(RequestIdMiddleware)
    origins = settings.cors_origin_list
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["accept", "content-type", "x-request-id"],
            expose_headers=["x-request-id"],
        )
    app.include_router(health_router)
    app.include_router(metrics_router)
    app.include_router(chat_router)
    app.include_router(conversations_router)
    app.include_router(operations_router)
    app.include_router(ui_router)
    return app


app = create_app()
