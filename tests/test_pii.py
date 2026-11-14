"""PII redaction and the query-logging policy."""
from __future__ import annotations

from blackbox.pii import redact_pii, summarize_query


def test_redacts_email_and_ip():
    text = "customer a@example.com called from 10.2.3.4"
    redacted = redact_pii(text)
    assert "a@example.com" not in redacted
    assert "10.2.3.4" not in redacted
    assert redacted.count("[redacted]") == 2


def test_redacts_bearer_and_provider_tokens():
    text = "Authorization: Bearer abcdef1234567890 key sk-abcdefghijklmnop"
    redacted = redact_pii(text)
    assert "abcdef1234567890" not in redacted
    assert "sk-abcdefghijklmnop" not in redacted


def test_redaction_is_stable_for_clean_text():
    text = "checkout-service p99 spiked"
    assert redact_pii(text) == text


def test_query_mode_hash_is_deterministic_and_opaque():
    first = summarize_query("checkout p99 spiked", "hash")
    second = summarize_query("checkout p99 spiked", "hash")
    assert first == second
    assert first.startswith("sha256:")
    assert "checkout" not in first


def test_query_mode_redact_masks_but_keeps_words():
    out = summarize_query("mail me at a@example.com about p99", "redact")
    assert "a@example.com" not in out
    assert "p99" in out


def test_query_mode_raw_is_verbatim():
    assert summarize_query("checkout p99", "raw") == "checkout p99"


def test_empty_query_is_empty():
    assert summarize_query("", "hash") == ""
