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


def test_readiness_reports_each_dependency(client):
    body = client.get("/health/ready").json()
    assert body["dependencies"]["storage"] == "ok"
    assert set(body["dependencies"]) >= {
        "storage",
        "runtime",
        "retrieval",
        "llm",
        "cache",
    }


def test_readiness_fails_when_the_database_ping_fails(client):
    services = client.app.state.services

    def down() -> None:
        raise RuntimeError("database is unreachable")

    services.conversations.ping = down
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["dependencies"]["storage"] == "down"


def test_request_id_echoed(client):
    r = client.get("/health", headers={"X-Request-ID": "abc-999"})
    assert r.headers.get("x-request-id") == "abc-999"


def test_request_id_generated(client):
    r = client.get("/health")
    assert r.headers.get("x-request-id")
    assert len(r.headers.get("x-request-id")) > 0
