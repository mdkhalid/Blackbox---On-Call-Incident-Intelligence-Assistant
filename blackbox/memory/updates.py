"""Deterministic entity-memory updates from user turns.

No LLM call is used here on purpose: hypothesis status changes must come from explicit user
statements, never from the assistant confirming its own guess. The assistant's grounded
claims are already handled by the graph; this module only records what the engineer said.

Conflict resolution falls out of `EntityMemory.upsert_hypothesis`: "we ruled out the deploy"
followed by "actually the deploy was the cause" updates one hypothesis in place and keeps an
audit entry instead of holding two contradictory active states.
"""
from __future__ import annotations

import re

from blackbox.memory.models import EntityMemory

SERVICE_RE = re.compile(r"\b[a-z][a-z0-9]*(?:-[a-z0-9]+)+\b", re.IGNORECASE)
ERROR_CODE_RE = re.compile(r"\bERR_[A-Z_]+\b")
TIMELINE_RE = re.compile(r"\b(?:\d{1,2}:\d{2}|\d{4}-\d{2}-\d{2})\b")
ACTION_RE = re.compile(
    r"\b(checked|restarted|rolled\s+back|scaled|increased|decreased|disabled|enabled|"
    r"reverted|deployed)\s+([^.,;!?]{3,80})",
    re.IGNORECASE,
)

# Order matters: ruled-out phrasing is checked before confirmed phrasing so "we ruled out X"
# cannot be read as a confirmation.
_RULED_OUT = [
    re.compile(
        r"\bruled\s+out\s+(?:the\s+)?(?P<h>.+?)(?:\s+as\s+(?:the\s+)?cause|\s+because|[,.;!?]|$)",
        re.IGNORECASE,
    ),
    re.compile(r"(?P<h>[\w\s\-:]+?)\s+(?:is|was)\s+ruled\s+out", re.IGNORECASE),
    re.compile(
        r"(?P<h>[\w\s\-:]+?)\s+(?:wasn't|was\s+not|isn't|is\s+not)\s+the\s+cause",
        re.IGNORECASE,
    ),
]
_CONFIRMED = [
    re.compile(
        r"\bactually,?\s+(?:the\s+)?(?P<h>.+?)\s+(?:was|is)\s+the\s+cause",
        re.IGNORECASE,
    ),
    re.compile(
        r"\broot\s+cause\s+(?:was|is)\s+(?:the\s+)?(?P<h>.+?)(?:\s+in\s+\S+)?[.;!?]?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bconfirmed(?:\s+that)?\s*:?\s+(?:the\s+)?(?P<h>.+?)(?:\s+was|\s+is|[,.;!?]|$)",
        re.IGNORECASE,
    ),
    re.compile(r"(?P<h>[\w\s\-:]+?)\s+(?:was|is)\s+confirmed", re.IGNORECASE),
]

# Service names in this domain end with an infrastructure suffix. Using an allowlist
# avoids treating phrases like "code-review" or "read-only" as services.
_SERVICE_SUFFIXES = (
    "-service",
    "-gateway",
    "-api",
    "-db",
    "-cache",
    "-worker",
    "-queue",
    "-store",
    "-proxy",
    "-engine",
    "-cluster",
)

# Articles are stripped so "The deploy" and "deploy" are the same hypothesis.
_LEADING_ARTICLES = ("the ", "a ", "an ")


def _clean_hypothesis(value: str) -> str:
    cleaned = value.strip(" \t\r\n.,;:!?-—")
    lowered = cleaned.lower()
    for article in _LEADING_ARTICLES:
        if lowered.startswith(article):
            cleaned = cleaned[len(article) :]
            break
    return cleaned.strip(" \t\r\n.,;:!?-—")


def _services(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for token in SERVICE_RE.findall(text):
        lowered = token.lower()
        if not lowered.endswith(_SERVICE_SUFFIXES):
            continue
        if lowered not in seen:
            seen.add(lowered)
            found.append(token)
    return found


def extract_hypotheses(text: str) -> list[tuple[str, str]]:
    """Return `(text, status)` pairs from explicit user statements."""
    statements: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for pattern in _RULED_OUT:
        for match in pattern.finditer(text):
            hypothesis = _clean_hypothesis(match.group("h"))
            if _valid(hypothesis):
                key = (hypothesis.lower(), "ruled_out")
                if key not in seen:
                    seen.add(key)
                    statements.append((hypothesis, "ruled_out"))
    for pattern in _CONFIRMED:
        for match in pattern.finditer(text):
            hypothesis = _clean_hypothesis(match.group("h"))
            if _valid(hypothesis):
                key = (hypothesis.lower(), "confirmed")
                if key not in seen:
                    seen.add(key)
                    statements.append((hypothesis, "confirmed"))
    return statements


def _valid(hypothesis: str) -> bool:
    if not 3 <= len(hypothesis) <= 120:
        return False
    return hypothesis.lower() not in {"it", "that", "this", "there", "the cause"}


def update_entity_memory(
    entity: EntityMemory,
    *,
    user_message: str,
    assistant_message: str = "",
) -> list[str]:
    """Update services, error codes, timeline anchors, actions and hypotheses.

    Only the user's message is read for hypothesis status changes. The assistant's answer is
    accepted for the signature's future use but deliberately not used to confirm a guess.
    Returns human-readable event labels for logging.
    """
    events: list[str] = []

    services = _services(user_message)
    if services:
        before = set(entity.services)
        entity.note_services(services)
        added = [service for service in services if service not in before]
        events.extend(f"service:{service}" for service in added)

    codes = sorted(set(ERROR_CODE_RE.findall(user_message)))
    if codes:
        before = set(entity.error_codes)
        entity.note_error_codes(codes)
        events.extend(f"error_code:{code}" for code in codes if code not in before)

    anchors = list(dict.fromkeys(TIMELINE_RE.findall(user_message)))
    if anchors:
        before = set(entity.timeline_anchors)
        for anchor in anchors:
            entity.add_timeline_anchor(anchor)
        events.extend(f"timeline:{anchor}" for anchor in anchors if anchor not in before)

    for match in ACTION_RE.finditer(user_message):
        action = f"{match.group(1)} {match.group(2)}".strip()
        prior_actions = {(item.action, item.outcome) for item in entity.actions}
        entity.add_action(action)
        if (action, "") not in prior_actions:
            events.append(f"action:{action}")

    for hypothesis, status in extract_hypotheses(user_message):
        existing = next(
            (
                item
                for item in entity.hypotheses
                if _normalized(item.text) == _normalized(hypothesis)
            ),
            None,
        )
        previous_status = existing.status if existing else None
        entity.upsert_hypothesis(hypothesis, status=status)
        if previous_status != status:
            events.append(f"hypothesis:{status}:{hypothesis}")

    return events


def _normalized(text: str) -> str:
    tokens = re.findall(r"[a-z0-9_]+", text.lower())
    while tokens and tokens[0] in {"the", "a", "an"}:
        tokens.pop(0)
    return " ".join(tokens)
