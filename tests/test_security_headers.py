"""Security headers are present on every response, including errors."""
from __future__ import annotations


def test_health_carries_security_headers(client):
    response = client.get("/health")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "camera=()" in response.headers["permissions-policy"]


def test_error_response_carries_security_headers(client):
    response = client.get("/api/conversations/missing")
    assert response.status_code == 404
    assert response.headers["x-content-type-options"] == "nosniff"


def test_streaming_response_carries_security_headers(client):
    # The middleware is pure ASGI; a streaming response must still get the headers.
    response = client.post("/api/chat", json={"message": "hello"})
    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
