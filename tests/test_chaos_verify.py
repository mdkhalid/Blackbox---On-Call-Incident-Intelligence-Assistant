"""chaos_verify turns a drill into a pass/fail verdict."""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "chaos_verify", ROOT / "scripts" / "chaos_verify.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_verdict_passes_when_healthy(monkeypatch):
    module = _load()

    def fake_get(url, timeout=5.0):
        if url.endswith("/metrics"):
            return 200, b"blackbox_build_info 1"
        return 200, b"{}"

    def fake_post(url, payload, timeout=30.0):
        return 200, b"event: final\ndata: {}\n"

    monkeypatch.setattr(module, "_get", fake_get)
    monkeypatch.setattr(module, "_post_json", fake_post)

    result = module.run("http://example")
    assert result["passed"] is True
    assert result["checks"]["chat_within_slo"] is True


def test_verdict_fails_on_server_error(monkeypatch):
    module = _load()

    monkeypatch.setattr(module, "_get", lambda url, timeout=5.0: (200, b"{}"))
    monkeypatch.setattr(module, "_post_json", lambda url, payload, timeout=30.0: (500, b""))

    result = module.run("http://example")
    assert result["passed"] is False
    assert result["checks"]["no_5xx"] is False
