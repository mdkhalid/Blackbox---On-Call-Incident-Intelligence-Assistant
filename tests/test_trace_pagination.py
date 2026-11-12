"""Paginated /trace, including the archived window."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from blackbox.api.services import AppServices
from blackbox.api.store import SqlAlchemyConversationRepository
from blackbox.config.settings import AppConfig, get_settings
from blackbox.main import create_app


@pytest.fixture
def paged(tmp_path, monkeypatch):
    monkeypatch.setenv("TRACE_ARCHIVE_BACKEND", "file")
    monkeypatch.setenv("TRACE_ARCHIVE_DIR", str(tmp_path / "archive"))
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("REDIS_URL", "")
    settings = get_settings()
    config = AppConfig()
    repo = SqlAlchemyConversationRepository(
        f"sqlite:///{tmp_path / 'conv.db'}", memory_config=config.memory
    )
    repo.init()
    services = AppServices(
        settings=settings,
        config=config,
        conversations=repo,
        storage_ready=True,
    )
    return TestClient(create_app(settings, services=services)), repo, services


def _seed(repo, turns: int = 5) -> str:
    repo.get_or_create("c1")
    for i in range(turns):
        repo.append_turn("c1", f"q{i}", f"a{i}", "ok", [], {}, tenant_id="default")
    return "c1"


def test_trace_is_paginated_oldest_first(paged):
    client, repo, _services = paged
    _seed(repo)

    first = client.get("/api/conversations/c1/trace", params={"limit": 2}).json()
    assert first["total"] == 5
    assert first["returned"] == 2
    assert first["has_more"] is True
    assert [turn["user_message"] for turn in first["turns"]] == ["q0", "q1"]

    last = client.get(
        "/api/conversations/c1/trace", params={"limit": 2, "offset": 4}
    ).json()
    assert last["returned"] == 1
    assert last["has_more"] is False
    assert [turn["user_message"] for turn in last["turns"]] == ["q4"]


def test_trace_page_validation(paged):
    client, repo, _services = paged
    _seed(repo, 1)

    assert (
        client.get("/api/conversations/c1/trace", params={"limit": 0}).status_code == 400
    )
    assert (
        client.get("/api/conversations/c1/trace", params={"limit": 501}).status_code
        == 400
    )
    assert (
        client.get("/api/conversations/c1/trace", params={"offset": -1}).status_code
        == 400
    )
    assert client.get("/api/conversations/missing/trace").status_code == 404


def test_include_archive_merges_the_cold_window(paged):
    client, repo, services = paged
    _seed(repo, 3)
    archive = services.get_trace_archive()
    archive.write(
        "default",
        "c1",
        [
            {
                "id": 900,
                "conversation_id": "c1",
                "tenant_id": "default",
                "user_message": "archived question",
                "assistant_message": "archived answer",
                "status": "ok",
                "trace": [],
                "budget": {},
                "chunks": [],
                "created_at": "2020-01-01T00:00:00+00:00",
            }
        ],
    )

    body = client.get(
        "/api/conversations/c1/trace",
        params={"limit": 2, "offset": 0, "include_archive": "true"},
    ).json()
    assert body["total"] == 4
    assert body["returned"] == 2
    assert body["has_more"] is True
    assert [turn["user_message"] for turn in body["turns"]] == [
        "archived question",
        "q0",
    ]

    tail = client.get(
        "/api/conversations/c1/trace",
        params={"limit": 10, "offset": 2, "include_archive": "true"},
    ).json()
    assert [turn["user_message"] for turn in tail["turns"]] == ["q1", "q2"]
    assert tail["has_more"] is False


def test_include_archive_is_ignored_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("TRACE_ARCHIVE_BACKEND", "none")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("REDIS_URL", "")
    settings = get_settings()
    config = AppConfig()
    repo = SqlAlchemyConversationRepository(
        f"sqlite:///{tmp_path / 'conv2.db'}", memory_config=config.memory
    )
    repo.init()
    _seed(repo, 2)
    services = AppServices(
        settings=settings, config=config, conversations=repo, storage_ready=True
    )
    client = TestClient(create_app(settings, services=services))

    body = client.get(
        "/api/conversations/c1/trace",
        params={"limit": 10, "include_archive": "true"},
    ).json()
    assert body["total"] == 2
    assert [turn["user_message"] for turn in body["turns"]] == ["q0", "q1"]
