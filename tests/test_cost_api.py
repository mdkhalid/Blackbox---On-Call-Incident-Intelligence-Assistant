"""Cost endpoints expose the per-node breakdown."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from blackbox.api.services import AppServices
from blackbox.api.store import SqlAlchemyConversationRepository
from blackbox.config.settings import AppConfig, CostConfig, ModelPrice, get_settings
from blackbox.main import create_app


@pytest.fixture
def cost_services(tmp_path, monkeypatch):
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'cost.db'}")
    get_settings.cache_clear()
    settings = get_settings()
    repo = SqlAlchemyConversationRepository(f"sqlite:///{tmp_path / 'cost.db'}")
    repo.init()
    config = AppConfig(
        cost=CostConfig(
            models={
                "gpt-4o-mini": ModelPrice(input_per_1m=0.15, output_per_1m=0.60),
                "gpt-4o": ModelPrice(input_per_1m=2.50, output_per_1m=10.00),
            }
        )
    )
    return AppServices(
        settings=settings,
        config=config,
        conversations=repo,
        storage_ready=True,
        runtime_ready=True,
        storage_backend="sqlite",
    )


@pytest.fixture
def cost_client(cost_services):
    return TestClient(create_app(cost_services.settings, services=cost_services))


def _seed(services, conversation_id: str):
    services.conversations.get_or_create(conversation_id, tenant_id="default")
    trace = [
        {
            "node": "grade",
            "model": "gpt-4o-mini",
            "usage": {"prompt_tokens": 1000, "completion_tokens": 100},
        },
        {
            "node": "generate",
            "model": "gpt-4o",
            "usage": {"prompt_tokens": 2000, "completion_tokens": 500},
        },
    ]
    services.conversations.append_turn(
        conversation_id,
        "has this happened before?",
        "yes",
        "answered",
        trace,
        {},
        tenant_id="default",
    )


def test_conversation_cost_endpoint(cost_client, cost_services):
    _seed(cost_services, "conv-cost")

    response = cost_client.get("/api/conversations/conv-cost/cost")

    assert response.status_code == 200
    body = response.json()
    assert body["conversation_id"] == "conv-cost"
    assert body["turns"] == 1
    assert set(body["by_node"]) == {"grade", "generate"}
    assert body["total_usd"] > 0


def test_conversation_cost_unknown_is_404(cost_client):
    assert cost_client.get("/api/conversations/missing/cost").status_code == 404


def test_tenant_cost_endpoint(cost_client, cost_services):
    _seed(cost_services, "conv-a")
    _seed(cost_services, "conv-b")

    response = cost_client.get("/api/admin/cost?days=30")

    assert response.status_code == 200
    body = response.json()
    assert body["tenant_id"] == "default"
    assert body["turns"] == 2
    assert body["total_usd"] > 0
