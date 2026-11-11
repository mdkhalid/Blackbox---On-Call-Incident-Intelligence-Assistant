"""Ground check: trace each claim back to a retrieved chunk.

This is a structural check, not an LLM judgement. "Does this claim trace to a chunk" is a
property of the citation graph plus the text, so it is deterministic, free, and cannot
itself hallucinate. Unsupported claims are stripped from the answer, and an answer with no
supported claim is routed to honest failure rather than shipped.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from blackbox.text import content_tokens

DEFAULT_MIN_OVERLAP = 0.34


@dataclass(slots=True)
class ClaimVerdict:
    claim: str
    supported: bool
    sources: list[str] = field(default_factory=list)
    matched_chunk_ids: list[str] = field(default_factory=list)
    overlap: float = 0.0
    reason: str = ""


@dataclass(slots=True)
class GroundCheckResult:
    verdicts: list[ClaimVerdict] = field(default_factory=list)

    @property
    def supported_count(self) -> int:
        return sum(1 for verdict in self.verdicts if verdict.supported)

    @property
    def unsupported_count(self) -> int:
        return sum(1 for verdict in self.verdicts if not verdict.supported)


def verify_claims(
    claims: list[dict],
    chunks: list[dict],
    *,
    min_overlap: float = DEFAULT_MIN_OVERLAP,
) -> GroundCheckResult:
    by_id = {chunk.get("chunk_id"): chunk for chunk in chunks if chunk.get("chunk_id")}
    verdicts: list[ClaimVerdict] = []

    for claim in claims:
        text = str(claim.get("text") or "").strip()
        sources = [str(source) for source in claim.get("sources") or []]
        claim_terms = content_tokens(text)

        if not text:
            continue
        if not claim_terms:
            verdicts.append(
                ClaimVerdict(text, False, sources, reason="claim has no content terms")
            )
            continue

        candidates = [by_id[source] for source in sources if source in by_id]
        if sources and not candidates:
            verdicts.append(
                ClaimVerdict(text, False, sources, reason="cited sources were not retrieved")
            )
            continue
        if not candidates:
            candidates = list(chunks)

        best_chunk, best_overlap = _best_match(claim_terms, candidates)
        supported = best_chunk is not None and best_overlap >= min_overlap
        verdicts.append(
            ClaimVerdict(
                claim=text,
                supported=supported,
                sources=sources,
                matched_chunk_ids=(
                    [best_chunk["chunk_id"]] if best_chunk is not None and supported else []
                ),
                overlap=round(best_overlap, 4),
                reason=(
                    "claim terms appear in retrieved evidence"
                    if supported
                    else "claim terms do not appear in retrieved evidence"
                ),
            )
        )

    return GroundCheckResult(verdicts)


def strip_unsupported(answer: str, result: GroundCheckResult) -> str:
    stripped = answer or ""
    for verdict in result.verdicts:
        if verdict.supported:
            continue
        for variant in _variants(verdict.claim):
            stripped = stripped.replace(variant, " ")
    return re.sub(r"\s+", " ", stripped).strip()


def _best_match(claim_terms: list[str], chunks: list[dict]) -> tuple[dict | None, float]:
    claim_set = set(claim_terms)
    best_chunk: dict | None = None
    best_overlap = 0.0
    for chunk in chunks:
        chunk_terms = set(content_tokens(chunk.get("text") or ""))
        if not chunk_terms:
            continue
        overlap = len(claim_set & chunk_terms) / len(claim_set)
        if overlap > best_overlap:
            best_chunk, best_overlap = chunk, overlap
    return best_chunk, best_overlap


def _variants(claim: str) -> list[str]:
    variants = [claim, claim.rstrip("."), f"{claim.rstrip('.')}."]
    return list(dict.fromkeys(variant for variant in variants if variant))
