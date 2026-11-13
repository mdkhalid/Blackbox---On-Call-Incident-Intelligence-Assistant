"""Request middleware: request-id, structured logging and rate limiting."""
from __future__ import annotations

import time
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from blackbox.api.client_ip import resolve_client_ip
from blackbox.observability.metrics import record_http

REQUEST_ID_HEADER = "X-Request-ID"

_EXEMPT_PATHS = {
    "/",
    "/ui",
    "/health",
    "/health/ready",
    "/metrics",
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
        duration = time.perf_counter() - start
        log.info(
            "request",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=round(duration * 1000, 2),
        )
        # Label on the route template, not the raw path: a conversation id in the
        # path would create one time series per conversation and melt Prometheus.
        route = request.scope.get("route")
        template = getattr(route, "path", None) or "unmatched"
        record_http(request.method, template, response.status_code, duration)
        return response


class BodyLimitMiddleware(BaseHTTPMiddleware):
    """Reject oversized API bodies before JSON parsing.

    Chunked requests without Content-Length still need an ASGI/proxy limit; this guards the
    common case and keeps a single huge chat payload from being materialized in memory.
    """

    def __init__(self, app, *, max_bytes: int = 32768) -> None:
        super().__init__(app)
        self.max_bytes = max(1, max_bytes)

    async def dispatch(self, request: Request, call_next):
        if (
            request.method in {"POST", "PUT", "PATCH"}
            and request.url.path.startswith("/api/")
        ):
            raw = request.headers.get("content-length")
            if raw is not None:
                try:
                    length = int(raw)
                except ValueError:
                    return JSONResponse(
                        {"detail": "invalid Content-Length"}, status_code=400
                    )
                if length > self.max_bytes:
                    return JSONResponse(
                        {"detail": "request body too large"},
                        status_code=413,
                        headers={"X-Max-Body-Bytes": str(self.max_bytes)},
                    )
        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding/fixed window limiter per client IP.

    The limiter is Redis-backed when available (shared across pods) and falls back to an
    in-process limiter. Health and the viewer are exempt.

    The key is the *client* address, not the TCP peer address. Behind a trusted proxy
    the peer is the proxy for every client, so keying on it would make the limit
    cluster-wide instead of per client.
    """

    def __init__(self, app, *, services, window_s: float = 1.0) -> None:
        super().__init__(app)
        self.services = services
        self.window_s = window_s

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if request.method == "OPTIONS" or path in _EXEMPT_PATHS or path.startswith("/health"):
            return await call_next(request)

        key = resolve_client_ip(request, self.services.settings.trusted_proxy_networks)
        limit = self.services.config.guardrails.rate_limit_rps
        limiter = await self.services.ensure_rate_limiter()
        allowed, retry_after = await limiter.check(
            key, limit=limit, window_s=self.window_s
        )
        if not allowed:
            return JSONResponse(
                {"detail": "rate limit exceeded"},
                status_code=429,
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(limit),
                },
            )
        return await call_next(request)
