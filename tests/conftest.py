import pytest
from fastapi.testclient import TestClient

from blackbox.config.settings import get_settings
from blackbox.main import create_app


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    monkeypatch.setenv("CONFIG_PATH", "blackbox/config/config.yaml")
    return get_settings()


@pytest.fixture
def client(settings):
    app = create_app(settings)
    return TestClient(app)
