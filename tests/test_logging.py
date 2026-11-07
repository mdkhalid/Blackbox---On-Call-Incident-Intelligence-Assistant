import json

import structlog

from blackbox.config.settings import Settings
from blackbox.logging_setup import setup_logging


def test_logging_json_in_prod(monkeypatch, capsys):
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    s = Settings()
    setup_logging(s)
    log = structlog.get_logger()
    structlog.contextvars.bind_contextvars(request_id="req-123")
    log.info("hello", foo=1)
    structlog.contextvars.clear_contextvars()
    captured = capsys.readouterr()
    # find a JSON line
    lines = [ln for ln in captured.out.splitlines() if ln.strip()]
    assert lines, "no log output"
    parsed = None
    for ln in lines:
        try:
            parsed = json.loads(ln)
            break
        except json.JSONDecodeError:
            continue
    assert parsed is not None, f"no JSON log line: {captured.out}"
    assert parsed.get("request_id") == "req-123"
    assert parsed.get("event") == "hello"
    assert parsed.get("foo") == 1


def test_logging_console_in_dev(monkeypatch, capsys):
    monkeypatch.setenv("ENV", "dev")
    s = Settings()
    setup_logging(s)
    log = structlog.get_logger()
    log.info("devmsg")
    captured = capsys.readouterr()
    assert "devmsg" in (captured.out + captured.err)
