import pytest
from pydantic import ValidationError

from blackbox.config.settings import Settings, get_settings, load_app_config


def test_settings_defaults(monkeypatch):
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    s = Settings()
    assert s.env == "dev"
    assert s.log_level == "INFO"
    assert s.llm_provider == "openai"
    assert s.embedding_model == "text-embedding-3-small"


def test_settings_env_override(monkeypatch):
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_JWT_SECRET", "test-secret")
    s = Settings()
    assert s.env == "prod"
    assert s.log_level == "DEBUG"
    assert s.llm_provider == "anthropic"


def test_prod_requires_api_key(monkeypatch):
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValidationError):
        Settings()


def test_prod_with_key_ok(monkeypatch):
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_JWT_SECRET", "test-secret")
    s = Settings()
    assert s.openai_api_key == "sk-test"


def test_prod_requires_auth(monkeypatch):
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("AUTH_ENABLED", "false")
    with pytest.raises(ValidationError):
        Settings()


def test_invalid_log_level_not_strict():
    # log_level is free string; validation not strict by design
    s = Settings(LOG_LEVEL="whatever")
    assert s.log_level == "whatever"


def test_app_config_loads_yaml(monkeypatch, tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("retrieval:\n  top_k: 7\n  rewrite_cap: 3\n")
    monkeypatch.setenv("CONFIG_PATH", str(cfg_file))
    s = Settings()
    cfg = load_app_config(s)
    assert cfg.retrieval.top_k == 7
    assert cfg.retrieval.rewrite_cap == 3


def test_app_config_defaults_when_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "nope.yaml"))
    s = Settings()
    cfg = load_app_config(s)
    # defaults applied
    assert cfg.retrieval.top_k >= 1
    assert cfg.retrieval.rewrite_cap >= 1


def test_app_config_invalid_value_raises(monkeypatch, tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("retrieval:\n  top_k: 0\n")
    monkeypatch.setenv("CONFIG_PATH", str(cfg_file))
    s = Settings()
    with pytest.raises(ValueError):
        load_app_config(s)


def test_get_settings_cached(monkeypatch):
    monkeypatch.setenv("ENV", "dev")
    a = get_settings()
    b = get_settings()
    assert a is b
