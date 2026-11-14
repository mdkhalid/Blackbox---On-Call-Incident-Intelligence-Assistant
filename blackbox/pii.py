"""PII redaction and the query-logging policy.

Incident text legitimately contains identifiers, hostnames and sometimes customer
data. Logs are not the place for it. Two controls:

* `redact_pii` masks emails, IPs and bearer-style tokens in a log field.
* `summarize_query` decides what a query log records: a stable hash (default), a
  redacted copy, or the raw text when an operator explicitly opts in.

Hashing keeps queries correlatable — the same query yields the same hash — without
storing their content.
"""
from __future__ import annotations

import hashlib
import re

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_BEARER = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)
_TOKEN = re.compile(r"\b(?:sk|pk|ghp|xox[baprs])[-_][A-Za-z0-9_-]{10,}\b")

REDACTED = "[redacted]"


def redact_pii(text: str) -> str:
    """Mask the common identifiers that must not reach a log line."""
    if not text:
        return ""
    redacted = _EMAIL.sub(REDACTED, text)
    redacted = _IPV4.sub(REDACTED, redacted)
    redacted = _BEARER.sub(lambda match: f"{match.group(1)} {REDACTED}", redacted)
    redacted = _TOKEN.sub(REDACTED, redacted)
    return redacted


def summarize_query(text: str, mode: str) -> str:
    """What a query log records, per the configured policy.

    `hash` (default) stores a truncated SHA-256; `redact` stores the text with PII
    masked; `raw` stores it verbatim and is a deliberate operator choice.
    """
    if not text:
        return ""
    if mode == "raw":
        return text
    if mode == "redact":
        return redact_pii(text)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"sha256:{digest[:16]}"
