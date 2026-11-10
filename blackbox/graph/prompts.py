"""Prompt templates for the corrective-RAG graph.

The offline provider parses these exact templates, so the prompt shape is part of the
system contract rather than an implementation detail.
"""
from __future__ import annotations

from collections.abc import Iterable

GRADE_SYSTEM = (
    "You are a strict retrieval grader for an on-call incident assistant. "
    "Decide only whether the supplied evidence contains enough information to answer the "
    "question. Do not use outside knowledge. Reply with JSON."
)

REWRITE_SYSTEM = (
    "You rewrite search queries for an incident knowledge base. Preserve error codes, "
    "service names, dates and other identifiers exactly. Reply with the rewritten query "
    "only, no explanation."
)

GENERATE_SYSTEM = (
    "You answer on-call questions using only the supplied evidence from past incidents. "
    "Cite every factual claim with the chunk id in square brackets. If the evidence does "
    "not answer the question, return an empty answer instead of guessing. Reply with JSON."
)


def evidence_lines(chunks: Iterable[dict]) -> str:
    lines = []
    for chunk in chunks:
        chunk_id = chunk.get("chunk_id", "unknown")
        lines.append(f"[{chunk_id}] {chunk.get('text', '')}")
    return "\n".join(lines)


def grade_prompt(query: str, chunks: list[dict]) -> str:
    return (
        "TASK: GRADE\n"
        f"QUERY: {query}\n"
        "EVIDENCE:\n"
        f"{evidence_lines(chunks)}\n\n"
        'Return JSON: {"sufficient": true|false, "reason": "short explanation"}'
    )


def rewrite_prompt(query: str, attempt: int) -> str:
    return (
        "TASK: REWRITE\n"
        f"QUESTION: {query}\n"
        f"ATTEMPT: {attempt}\n"
        "Rewrite the question into search terms that will retrieve relevant incident "
        "documents. Keep identifiers intact; drop conversational filler."
    )


def generate_prompt(query: str, chunks: list[dict]) -> str:
    return (
        "TASK: GENERATE\n"
        f"QUESTION: {query}\n"
        "EVIDENCE:\n"
        f"{evidence_lines(chunks)}\n\n"
        'Return JSON: {"answer": "...", "claims": [{"text": "...", "sources": ["<chunk_id>"]}]}'
    )
