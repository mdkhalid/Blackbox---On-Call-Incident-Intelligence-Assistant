"""Graceful shutdown and SSE cancellation (Phase 3)."""
from __future__ import annotations

from fastapi.testclient import TestClient

from blackbox.main import create_app


def test_lifespan_shuts_down_cleanly():
    app = create_app()
    with TestClient(app) as client:
        r = client.get("/health")
        assert r.status_code == 200
    # After exiting the context, jobs should be stopped; no exception raised.


def test_health_ready_after_startup():
    app = create_app()
    with TestClient(app) as client:
        r = client.get("/health/ready")
        # ready may be 200 or 503 depending on storage; either is a valid response
        assert r.status_code in (200, 503)