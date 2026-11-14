"""Request middleware: request-id, structured logging and rate limiting."""
from __future__ import annotations

import time
import uuid

import structlog
from starlette.datastructures import MutableHeaders
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from blackbox.api.client_ip import resolve_client_ip
from blackbox.observability.metrics import record_http
from blackbox.observability.tracing import current_trace_id, span

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


_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


class SecurityHeadersMiddleware:
    """Pure-ASGI security headers.

    A `BaseHTTPMiddleware` would wrap the SSE response and is known to interfere with
    streaming; mutating `http.response.start` keeps the stream untouched. No CSP is
    set: the trace viewer is a single file with inline script, and a policy that
    silently broke it would be worse than an explicit absence.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in _SECURITY_HEADERS.items():
                    headers.setdefault(name, value)
            await send(message)

        await self.app(scope, receive, send_with_headers)


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
        structlog.contextvars.clear_contextvars()
        trace_id = current_trace_id()
        structlog.contextvars.bind_contextvars(
            request_id=rid, **({"trace_id": trace_id} if trace_id else {})
        )
        response: Response = await call_next(request)
        response.headers[REQUEST_ID_HEADER.lower()] = rid
        structlog.contextvars.clear_contextvars()
        return response


class TracingMiddleware:
    """Pure-ASGI tracing so a streamed response stays inside its request span.

    A ``BaseHTTPMiddleware`` span ends when ``call_next`` returns, which for SSE is
    before the body is produced. Wrapping ``send`` keeps the span open for the whole
    response and lets the route template (known only after routing) name the span.
    """

    def __init__(self, app, *, ignore_paths: tuple[str, ...] = ()) -> None:
        self.app = app
        self.ignore_paths = frozenset(ignore_paths)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") in self.ignore_paths:
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET")
        with span("http.request", **{"http.method": method}) as current:
            async def send_with_route(message):
                if message["type"] == "http.response.start":
                    status = int(message["status"])
                    current.set_attribute("http.status_code", status)
                    route = scope.get("route")
                    template = getattr(route, "path", None)
                    if template:
                        # Attribute and span name use the route template, never the
                        # raw path: a conversation id in the URL must not become a
                        # distinct span name.
                        current.set_attribute("http.route", template)
                        current.set_name(f"{method} {template}")
                await send(message)

            try:
                await self.app(scope, receive, send_with_route)
            except Exception as exc:
                current.record_exception(exc)
                raise



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
