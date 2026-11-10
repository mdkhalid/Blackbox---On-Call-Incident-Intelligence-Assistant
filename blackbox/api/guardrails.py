"""Input guardrails: prompt-injection heuristics.

Flagging is the default because incident text is legitimate and can mention "system" or
"instructions". Strict blocking is opt-in via `guardrails.strict_injection_block`.
"""
from __future__ import annotations

import re

_INJECTION_PATTERNS = [
    ("ignore_instructions", re.compile(r"\bignore\s+(all\s+)?(previous|prior|above)\s+instructions\b", re.IGNORECASE)),
    ("disregard_instructions", re.compile(r"\bdisregard\s+(the\s+)?(system|previous|above)\b", re.IGNORECASE)),
    ("system_prompt_request", re.compile(r"\b(reveal|show|print|repeat)\b.{0,30}\b(system\s+prompt|instructions)\b", re.IGNORECASE)),
    ("role_override", re.compile(r"\b(you\s+are\s+now|act\s+as|pretend\s+to\s+be)\b", re.IGNORECASE)),
    ("jailbreak", re.compile(r"\b(jailbreak|developer\s+mode|DAN\s+mode)\b", re.IGNORECASE)),
]


def detect_injection(text: str) -> list[str]:
    """Return matched heuristic labels; empty when nothing matched."""
    matches: list[str] = []
    for label, pattern in _INJECTION_PATTERNS:
        if pattern.search(text or ""):
            matches.append(label)
    return matches
