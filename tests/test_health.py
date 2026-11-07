def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "version" in body
    assert "env" in body


def test_health_ready(client):
    r = client.get("/health/ready")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_request_id_echoed(client):
    r = client.get("/health", headers={"X-Request-ID": "abc-999"})
    assert r.headers.get("x-request-id") == "abc-999"


def test_request_id_generated(client):
    r = client.get("/health")
    assert r.headers.get("x-request-id")
    assert len(r.headers.get("x-request-id")) > 0
