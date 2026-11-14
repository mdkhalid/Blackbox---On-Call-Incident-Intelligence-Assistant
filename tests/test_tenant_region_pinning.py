"""Tenant home-region resolution and the 421 write-pinning guard."""
import time

import jwt
import pytest
from fastapi.testclient import TestClient

from blackbox.api.regions import TenantRegionResolver, WrongRegion
from blackbox.api.services import AppServices
from blackbox.api.store import SqlAlchemyConversationRepository
from blackbox.config.settings import AppConfig, get_settings
from blackbox.main import create_app

SECRET = "region-test-secret-0123456789abcdef"
ISSUER = "blackbox-test"
AUDIENCE = "blackbox-api"


def _token(tenant: str) -> str:
    payload = {
        "sub": "user-1",
        "tenant_id": tenant,
        "roles": ["operator"],
        "iss": ISSUER,
        "aud": AUDIENCE,
        "exp": int(time.time()) + 3600,
    }
    return jwt.encode(payload, SECRET, algorithm="HS256")


def _header(tenant: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(tenant)}"}


@pytest.fixture
def region_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_JWT_SECRET", SECRET)
    monkeypatch.setenv("AUTH_JWT_ISSUER", ISSUER)
    monkeypatch.setenv("AUTH_JWT_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("REGION", "eu-west-1")
    monkeypatch.setenv("DEFAULT_HOME_REGION", "eu-west-1")
    monkeypatch.setenv("REGION_WRITE_PINNING", "true")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'region.db'}")
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture
def region_services(tmp_path, region_settings):
    repo = SqlAlchemyConversationRepository(
        f"sqlite:///{tmp_path / 'region.db'}",
        region=region_settings.region,
        home_region=region_settings.default_home_region,
    )
    repo.init()
    return AppServices(
        settings=region_settings,
        config=AppConfig(),
        conversations=repo,
        storage_ready=True,
        runtime_ready=True,
        storage_backend="sqlite",
    )


@pytest.fixture
def region_client(region_settings, region_services):
    return TestClient(create_app(region_settings, services=region_services))


# ---- resolver unit tests -------------------------------------------------------


def test_unmapped_tenant_uses_default_region(region_services):
    resolver = TenantRegionResolver(
        region_services.conversations,
        current_region="eu-west-1",
        default_region="eu-west-1",
        cache_ttl_s=0,
    )
    assert resolver.home_region("never-seen") == "eu-west-1"


def test_mapped_tenant_wrong_region_raises(region_services):
    region_services.conversations.set_home_region("tenant-b", "us-east-1")
    resolver = TenantRegionResolver(
        region_services.conversations,
        current_region="eu-west-1",
        default_region="eu-west-1",
        cache_ttl_s=0,
    )
    with pytest.raises(WrongRegion) as exc:
        resolver.assert_write_region("tenant-b")
    assert exc.value.home_region == "us-east-1"


def test_move_invalidates_cache(region_services):
    resolver = TenantRegionResolver(
        region_services.conversations,
        current_region="us-east-1",
        default_region="eu-west-1",
        cache_ttl_s=60,
    )
    assert resolver.home_region("tenant-c") == "eu-west-1"
    resolver.set_home_region("tenant-c", "us-east-1")
    assert resolver.home_region("tenant-c") == "us-east-1"


# ---- HTTP tests ----------------------------------------------------------------


def test_conversation_read_in_non_home_region_is_421(region_client, region_services):
    region_services.conversations.set_home_region("tenant-b", "us-east-1")
    region_services.conversations.get_or_create("conv-1", tenant_id="tenant-b")

    response = region_client.get("/api/conversations/conv-1", headers=_header("tenant-b"))

    assert response.status_code == 421
    assert response.headers["x-blackbox-home-region"] == "us-east-1"


def test_chat_in_non_home_region_is_421(region_client, region_services):
    region_services.conversations.set_home_region("tenant-b", "us-east-1")

    response = region_client.post(
        "/api/chat",
        json={"message": "has this happened before?"},
        headers=_header("tenant-b"),
    )

    assert response.status_code == 421
    assert response.headers["x-blackbox-home-region"] == "us-east-1"


def test_home_region_request_is_served(region_client, region_services):
    region_services.conversations.get_or_create("conv-2", tenant_id="tenant-a")

    response = region_client.get("/api/conversations/conv-2", headers=_header("tenant-a"))

    assert response.status_code == 200
    assert "x-blackbox-home-region" not in response.headers


def test_pinning_disabled_skips_guard(monkeypatch, tmp_path):
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("REGION", "eu-west-1")
    monkeypatch.setenv("REGION_WRITE_PINNING", "false")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'nopin.db'}")
    get_settings.cache_clear()
    settings = get_settings()
    repo = SqlAlchemyConversationRepository(f"sqlite:///{tmp_path / 'nopin.db'}")
    repo.init()
    repo.get_or_create("conv-3", tenant_id="default")
    services = AppServices(
        settings=settings,
        config=AppConfig(),
        conversations=repo,
        storage_ready=True,
        runtime_ready=True,
        storage_backend="sqlite",
    )
    client = TestClient(create_app(settings, services=services))

    assert client.get("/api/conversations/conv-3").status_code == 200


def test_conversation_records_its_region(region_services):
    record = region_services.conversations.get_or_create("conv-4", tenant_id="tenant-a")
    assert record.region == "eu-west-1"
    assert record.home_region == "eu-west-1"
