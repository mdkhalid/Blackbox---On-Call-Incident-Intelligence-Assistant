def test_trace_viewer_is_served_at_root(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    body = response.text
    assert "Blackbox Trace Viewer" in body
    assert "/api/conversations/" in body
    assert "getReader" in body


def test_trace_viewer_alias(client):
    response = client.get("/ui")
    assert response.status_code == 200
    assert "Blackbox Trace Viewer" in response.text


def test_health_still_works_alongside_the_viewer(client):
    assert client.get("/health").status_code == 200
