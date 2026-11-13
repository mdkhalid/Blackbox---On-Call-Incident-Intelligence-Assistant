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
    TracingMiddleware,
)
from blackbox.api.routes.chat import router as chat_router
from blackbox.api.routes.conversations import router as conversations_router
from blackbox.api.routes.health import router as health_router
from blackbox.api.routes.metrics import router as metrics_router
from blackbox.api.routes.operations import router as operations_router
from blackbox.api.routes.ui import router as ui_router
from blackbox.api.services import AppServices, build_services
from blackbox.config.settings import Settings, get_settings
from blackbox.logging_setup import get_logger, setup_logging
from blackbox.observability.metrics import set_build_info
from blackbox.observability.tracing import configure_tracing, shutdown_tracing

logger = get_logger(__name__)


def create_app(settings: Settings | None = None, *, services: AppServices | None = None):
    if settings is None:
        settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.settings = settings
        app.state.services = services or build_services(settings)
        app.state.warmup_attempted = False
        app.state.warmup_error = None
        setup_logging(settings)
        if settings.sentry_dsn:
            try:
                import sentry_sdk

                sentry_sdk.init(
                    dsn=settings.sentry_dsn,
                    environment=settings.sentry_environment or settings.env,
                    release=__version__,
                    traces_sample_rate=settings.sentry_traces_sample_rate,
                )
                logger.info("sentry_enabled")
            except ImportError:  # pragma: no cover - optional dependency
                logger.warning(
                    "sentry_sdk_unavailable",
                    hint="pip install sentry-sdk",
                )
        configure_tracing(settings)
        set_build_info(__version__, settings.env)
        # Warm up the runtime (retriever + LLM) before serving traffic so the
        # first chat does not pay cold start. A failure is logged but does not
        # stop the pod; /health/ready reports the runtime state.
        app.state.warmup_attempted = True
        try:
            await asyncio.to_thread(app.state.services.ensure_runtime)
        except Exception as exc:  # noqa: BLE001 - warm-up is best effort
            app.state.warmup_error = str(exc)
            logger.warning("runtime_warmup_failed", error=str(exc))
        # LangGraph offloads...
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
            shutdown_tracing()
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
    # Added last, so it is the outermost layer: the request span covers middleware,
    # routing and the full streamed response body.
    app.add_middleware(
        TracingMiddleware, ignore_paths=tuple(settings.otel_ignore_path_list)
    )
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
