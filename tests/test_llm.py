import json

import pytest

from blackbox.config.settings import get_settings
from blackbox.graph.prompts import generate_prompt, grade_prompt, rewrite_prompt
from blackbox.llm import FakeLLMProvider, build_llm_provider

CHUNK = {
    "chunk_id": "c1",
    "doc_id": "pm-001",
    "section": "Root Cause",
    "text": "The root cause was ERR_POOL_EXHAUSTED in checkout-service. The pool was saturated.",
}


def test_fake_grade_marks_overlapping_evidence_sufficient():
    resp = FakeLLMProvider().complete(
        system="",
        prompt=grade_prompt("What caused ERR_POOL_EXHAUSTED in checkout-service?", [CHUNK]),
        json_mode=True,
    )
    assert json.loads(resp.text)["sufficient"] is True


def test_fake_grade_marks_unrelated_evidence_insufficient():
    resp = FakeLLMProvider().complete(
        system="",
        prompt=grade_prompt("What is the capital of France?", [CHUNK]),
        json_mode=True,
    )
    assert json.loads(resp.text)["sufficient"] is False


def test_fake_rewrite_keeps_identifiers_and_changes_query():
    query = "checkout is failing with ERR_POOL_EXHAUSTED"
    resp = FakeLLMProvider().complete(system="", prompt=rewrite_prompt(query, 1))
    assert "ERR_POOL_EXHAUSTED" in resp.text
    assert resp.text.strip() != query


def test_fake_generate_cites_evidence():
    resp = FakeLLMProvider().complete(
        system="", prompt=generate_prompt("What caused the incident?", [CHUNK]), json_mode=True
    )
    data = json.loads(resp.text)
    assert data["answer"]
    assert data["claims"]
    assert data["claims"][0]["sources"] == ["c1"]


def test_fake_generate_without_evidence_is_empty():
    resp = FakeLLMProvider().complete(
        system="", prompt=generate_prompt("What caused the incident?", []), json_mode=True
    )
    data = json.loads(resp.text)
    assert data["answer"] == ""
    assert data["claims"] == []


def test_fake_tracks_calls_and_usage():
    fake = FakeLLMProvider()
    fake.complete(system="s", prompt="p")
    assert len(fake.calls) == 1
    assert fake.usage["total_tokens"] > 0


def test_build_provider_auto_without_keys_falls_back_to_fake(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("CONFIG_PATH", "blackbox/config/config.yaml")
    get_settings.cache_clear()
    assert isinstance(build_llm_provider(get_settings()), FakeLLMProvider)


def test_build_provider_openai_without_key_raises(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("CONFIG_PATH", "blackbox/config/config.yaml")
    get_settings.cache_clear()
    with pytest.raises(ValueError):
        build_llm_provider(get_settings(), provider="openai")


def test_build_provider_unknown_name_raises(monkeypatch):
    monkeypatch.setenv("CONFIG_PATH", "blackbox/config/config.yaml")
    get_settings.cache_clear()
    with pytest.raises(ValueError):
        build_llm_provider(get_settings(), provider="telepathy")
