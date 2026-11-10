"""Request middleware: request-id, structured logging and rate limiting."""
from __future__ import annotations

import time
import uuid
from collections import deque

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

REQUEST_ID_HEADER = "X-Request-ID"

_EXEMPT_PATHS = {
    "/",
    "/ui",
    "/health",
    "/health/ready",
    "/docs",
    "/openapi.json",
    "/redoc",
    "/favicon.ico",
}


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=rid)
        response: Response = await call_next(request)
        response.headers[REQUEST_ID_HEADER.lower()] = rid
        structlog.contextvars.clear_contextvars()
        return response


class LoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        log = structlog.get_logger("http")
        start = time.perf_counter()
        response: Response = await call_next(request)
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        log.info(
            "request",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=duration_ms,
        )
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """In-process sliding-window limiter per client IP.

    Single-instance by design; multi-instance deployments should move counters to Redis.
    Health and the viewer are exempt so probes and the demo UI never get throttled.
    """

    def __init__(self, app, *, rate_limit: int = 5, window_s: float = 1.0) -> None:
        super().__init__(app)
        self.rate_limit = max(1, rate_limit)
        self.window_s = window_s
        self._hits: dict[str, deque[float]] = {}

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if request.method == "OPTIONS" or path in _EXEMPT_PATHS or path.startswith("/health"):
            return await call_next(request)

        key = request.client.host if request.client else "unknown"
        now = time.monotonic()
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] >= self.window_s:
            hits.popleft()
        if len(hits) >= self.rate_limit:
            retry_after = max(1, int(self.window_s - (now - hits[0])) + 1)
            return JSONResponse(
                {"detail": "rate limit exceeded"},
                status_code=429,
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(self.rate_limit),
                },
            )
        hits.append(now)
        return await call_next(request)
