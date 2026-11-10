import time

import jwt
import pytest
from fastapi.testclient import TestClient

from blackbox.api.services import AppServices
from blackbox.api.store import (
    ConversationTenantMismatch,
    SqlAlchemyConversationRepository,
)
from blackbox.config.settings import AppConfig, get_settings
from blackbox.main import create_app

SECRET = "test-secret-0123456789abcdef0123456789"
ISSUER = "blackbox-test"
AUDIENCE = "blackbox-api"


@pytest.fixture
def auth_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_JWT_SECRET", SECRET)
    monkeypatch.setenv("AUTH_JWT_ISSUER", ISSUER)
    monkeypatch.setenv("AUTH_JWT_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'auth.db'}")
    monkeypatch.setenv("CONFIG_PATH", "blackbox/config/config.yaml")
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture
def auth_services(tmp_path, auth_settings):
    repo = SqlAlchemyConversationRepository(f"sqlite:///{tmp_path / 'auth.db'}")
    repo.init()
    return AppServices(
        settings=auth_settings,
        config=AppConfig(),
        conversations=repo,
        storage_ready=True,
        runtime_ready=True,
        storage_backend="sqlite",
    )


@pytest.fixture
def auth_client(auth_settings, auth_services):
    return TestClient(create_app(auth_settings, services=auth_services))


def make_token(
    *,
    tenant: str | None = "tenant-a",
    roles=("operator",),
    subject: str = "user-1",
    expires_in: int = 3600,
    **extra,
) -> str:
    payload = {
        "sub": subject,
        "roles": list(roles),
        "iss": ISSUER,
        "aud": AUDIENCE,
        "exp": int(time.time()) + expires_in,
        **extra,
    }
    if tenant is not None:
        payload["tenant_id"] = tenant
    return jwt.encode(payload, SECRET, algorithm="HS256")


def auth_header(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def seed_conversation(services, conversation_id: str, tenant: str) -> None:
    services.conversations.get_or_create(conversation_id, tenant_id=tenant)
    services.conversations.append_turn(
        conversation_id,
        "what happened?",
        "pool exhaustion",
        "answered",
        [],
        {},
        tenant_id=tenant,
    )


def test_missing_token_is_401(auth_client):
    response = auth_client.get("/api/conversations/c1")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_invalid_token_is_401(auth_client):
    response = auth_client.get(
        "/api/conversations/c1", headers=auth_header("not-a-jwt")
    )
    assert response.status_code == 401


def test_expired_token_is_401(auth_client):
    token = make_token(expires_in=-10)
    assert auth_client.get(
        "/api/conversations/c1", headers=auth_header(token)
    ).status_code == 401


def test_token_without_tenant_is_401(auth_client):
    token = make_token(tenant=None)
    assert auth_client.get(
        "/api/conversations/c1", headers=auth_header(token)
    ).status_code == 401


def test_tenant_isolation_on_read(auth_client, auth_services):
    seed_conversation(auth_services, "shared-id", "tenant-a")

    allowed = auth_client.get(
        "/api/conversations/shared-id",
        headers=auth_header(make_token(tenant="tenant-a")),
    )
    denied = auth_client.get(
        "/api/conversations/shared-id",
        headers=auth_header(make_token(tenant="tenant-b")),
    )

    assert allowed.status_code == 200
    assert allowed.json()["turns"] == 1
    assert denied.status_code == 404


def test_viewer_cannot_chat(auth_client):
    response = auth_client.post(
        "/api/chat",
        json={"message": "hello"},
        headers=auth_header(make_token(roles=("viewer",))),
    )
    assert response.status_code == 403


def test_viewer_cannot_ingest_or_eval(auth_client):
    headers = auth_header(make_token(roles=("viewer",)))
    assert auth_client.post("/api/ingest", json={}, headers=headers).status_code == 403
    assert auth_client.post("/api/eval/run", json={}, headers=headers).status_code == 403


def test_operator_cannot_ingest(auth_client):
    response = auth_client.post(
        "/api/ingest", json={}, headers=auth_header(make_token(roles=("operator",)))
    )
    assert response.status_code == 403


def test_cross_tenant_chat_is_404(auth_client, auth_services):
    seed_conversation(auth_services, "shared-id", "tenant-a")
    response = auth_client.post(
        "/api/chat",
        json={"message": "hello", "conversation_id": "shared-id"},
        headers=auth_header(make_token(tenant="tenant-b")),
    )
    assert response.status_code == 404


def test_cross_tenant_conversation_id_cannot_be_hijacked(auth_services):
    auth_services.conversations.get_or_create("shared-id", tenant_id="tenant-a")
    with pytest.raises(ConversationTenantMismatch):
        auth_services.conversations.get_or_create("shared-id", tenant_id="tenant-b")


def test_auth_disabled_uses_dev_principal(client):
    # The default test app runs with AUTH_ENABLED=false; unknown IDs are 404, not 401.
    assert client.get("/api/conversations/missing").status_code == 404


def test_role_claim_can_be_space_delimited(auth_client):
    token = jwt.encode(
        {
            "sub": "u",
            "tenant_id": "tenant-a",
            "scope": "viewer operator",
            "iss": ISSUER,
            "aud": AUDIENCE,
            "exp": int(time.time()) + 3600,
        },
        SECRET,
        algorithm="HS256",
    )
    response = auth_client.get(
        "/api/conversations/missing", headers=auth_header(token)
    )
    assert response.status_code == 404
