"""Defensive parsing for LLM output.

Malformed model output must never crash a turn or be read as a green light: an
unparseable grade defaults to "insufficient", and a non-JSON answer is recovered as
sentences with whatever citations it contains.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from blackbox.text import split_sentences

_CITATION_RE = re.compile(r"\[([^\]]+)\]")


@dataclass(slots=True)
class GradeDecision:
    sufficient: bool = False
    reason: str = ""


@dataclass(slots=True)
class GeneratedClaim:
    text: str
    sources: list[str] = field(default_factory=list)


@dataclass(slots=True)
class GeneratedAnswer:
    answer: str = ""
    claims: list[GeneratedClaim] = field(default_factory=list)


def parse_grade(text: str) -> GradeDecision:
    data = _json_object(text)
    if data is None:
        return GradeDecision(False, "unparseable grade response")
    if "sufficient" not in data:
        return GradeDecision(False, "grade response missing 'sufficient'")
    return GradeDecision(
        sufficient=bool(data.get("sufficient")),
        reason=str(data.get("reason") or ""),
    )


def parse_generation(text: str) -> GeneratedAnswer:
    text = (text or "").strip()
    if not text:
        return GeneratedAnswer()

    data = _json_object(text)
    if data is not None and "answer" in data:
        answer = str(data.get("answer") or "")
        claims = [
            GeneratedClaim(
                text=str(claim.get("text") or ""),
                sources=[str(source) for source in claim.get("sources") or []],
            )
            for claim in data.get("claims") or []
            if isinstance(claim, dict) and claim.get("text")
        ]
        return GeneratedAnswer(answer=answer, claims=claims)

    claims = []
    for sentence in split_sentences(text):
        claims.append(
            GeneratedClaim(text=sentence, sources=_CITATION_RE.findall(sentence))
        )
    return GeneratedAnswer(answer=text, claims=claims)


def _json_object(text: str) -> dict | None:
    candidate = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, re.DOTALL)
    if fenced:
        candidate = fenced.group(1)
    else:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            return None
        candidate = candidate[start : end + 1]
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None
