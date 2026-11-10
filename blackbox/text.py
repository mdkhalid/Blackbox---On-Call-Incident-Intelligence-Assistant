"""Shared text helpers: content-token extraction and sentence splitting.

Grounding, the offline LLM and grading all need to reason about *content* terms rather
than grammatical filler, so the stopword list lives in one place instead of three.
"""
from __future__ import annotations

import re

from blackbox.retrieval.sparse import tokenize

STOPWORDS = frozenset(
    """
    a about after again against all am an and any are as at be because been before being
    below between both but by can could did do does doing down during each few for from
    further had has have having he her here hers herself him himself his how i if in into
    is it its itself just me more most my myself no nor not now of off on once only or
    other our ours ourselves out over own same she should so some such than that the their
    theirs them themselves then there these they this those through to too under until up
    very was we were what when where which while who whom why will with would you your
    yours yourself yourselves
    """.split()  # noqa: SIM905 - a readable word list beats a 90-item literal
)

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


def content_tokens(text: str) -> list[str]:
    """Tokens that carry meaning, with identifiers kept whole by the shared tokenizer."""
    return [t for t in tokenize(text or "") if len(t) > 1 and t not in STOPWORDS]


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_RE.split((text or "").strip()) if s.strip()]
