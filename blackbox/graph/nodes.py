"""Graph nodes: retrieve, grade, rewrite, generate, ground check, honest failure.

Nodes are thin and observable: each one returns a partial state update plus a trace event,
so the same run can be replayed by the trace viewer and streamed by the API.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any

from blackbox.config.settings import AppConfig, Settings
from blackbox.graph.grounding import strip_unsupported, verify_claims
from blackbox.graph.output_guard import scrub_secrets
from blackbox.graph.parsing import parse_generation, parse_grade
from blackbox.graph.prompts import (
    GENERATE_SYSTEM,
    GRADE_SYSTEM,
    REWRITE_SYSTEM,
    generate_prompt,
    grade_prompt,
    rewrite_prompt,
)
from blackbox.llm.base import LLMProvider, LLMResponse
from blackbox.logging_setup import get_logger
from blackbox.memory.tokens import count_tokens
from blackbox.observability.callbacks import log_llm_call

logger = get_logger(__name__)


@dataclass
class GraphDeps:
    retriever: Any
    llm: LLMProvider
    config: AppConfig
    settings: Settings
    entity_store: Any | None = None


# ---- retrieve ----


def retrieve_node(state: dict, deps: GraphDeps) -> dict:
    started = time.perf_counter()
    query = state.get("rewritten_query") or state.get("query", "")
    filters = state.get("filters") or None

    chunks: list[dict] = []
    error = state.get("error")
    try:
        hits = deps.retriever.retrieve(
            query, top_k=deps.config.retrieval.top_k, filter=filters
        )
        chunks = [_serialize_chunk(hit) for hit in hits]
    except Exception as exc:  # noqa: BLE001 - a store failure must become an honest answer
        error = f"retrieval_failed:{type(exc).__name__}"
        logger.warning("retrieve_node_failed", query=query[:120], error=error)

    timings = dict(state.get("timings") or {})
    timings.update(getattr(deps.retriever, "last_timings", {}) or {})
    timings["last_retrieve_ms"] = _elapsed(started)

    return {
        "chunks": chunks,
        "timings": timings,
        "error": error,
        "trace": [
            {
                "node": "retrieve",
                "query": query,
                "filters": filters or {},
                "chunks": len(chunks),
                "duration_ms": _elapsed(started),
            }
        ],
    }


# ---- grade ----


def grade_node(state: dict, deps: GraphDeps) -> dict:
    started = time.perf_counter()
    history = list(state.get("grade_history") or [])
    attempt = len(history) + 1
    query = state.get("rewritten_query") or state.get("query", "")

    if _over_budget(state, deps):
        return {
            **_cap_payload("grade"),
            "grade": {
                "sufficient": False,
                "reason": "LLM call cap reached",
                "attempt": attempt,
            },
            "trace": [
                {
                    "node": "grade",
                    "attempt": attempt,
                    "sufficient": False,
                    "reason": "llm_call_cap",
                    "duration_ms": _elapsed(started),
                }
            ],
        }

    call_started = time.perf_counter()
    response = deps.llm.complete(
        system=GRADE_SYSTEM,
        prompt=grade_prompt(query, state.get("chunks") or []),
        model=deps.settings.grading_model,
        temperature=0.0,
        max_tokens=256,
        json_mode=True,
    )
    log_llm_call(
        logger, node="grade", response=response,
        duration_ms=(time.perf_counter() - call_started) * 1000,
    )
    decision = parse_grade(response.text)
    grade = {
        "sufficient": decision.sufficient,
        "reason": decision.reason,
        "attempt": attempt,
        "query": query,
    }

    return {
        "grade": grade,
        "grade_history": [*history, grade],
        "llm_calls": int(state.get("llm_calls", 0)) + 1,
        "usage": _merge_usage(state, response),
        "trace": [
            {
                "node": "grade",
                "attempt": attempt,
                "sufficient": decision.sufficient,
                "reason": decision.reason,
                "duration_ms": _elapsed(started),
            }
        ],
    }


# ---- rewrite ----


def rewrite_node(state: dict, deps: GraphDeps) -> dict:
    started = time.perf_counter()
    query = state.get("rewritten_query") or state.get("query", "")
    attempt = int(state.get("rewrite_count", 0)) + 1

    if _over_budget(state, deps):
        return {
            **_cap_payload("rewrite"),
            "trace": [
                {
                    "node": "rewrite",
                    "attempt": attempt,
                    "rewritten": None,
                    "reason": "llm_call_cap",
                    "duration_ms": _elapsed(started),
                }
            ],
        }

    call_started = time.perf_counter()
    response = deps.llm.complete(
        system=REWRITE_SYSTEM,
        prompt=rewrite_prompt(query, attempt),
        model=deps.settings.rewrite_model,
        temperature=0.3,
        max_tokens=256,
    )
    log_llm_call(
        logger, node="rewrite", response=response,
        duration_ms=(time.perf_counter() - call_started) * 1000,
    )
    rewritten = (response.text or "").strip() or query

    return {
        "query": rewritten,
        "rewritten_query": rewritten,
        "rewrite_count": attempt,
        "llm_calls": int(state.get("llm_calls", 0)) + 1,
        "usage": _merge_usage(state, response),
        "trace": [
            {
                "node": "rewrite",
                "attempt": attempt,
                "from": query,
                "rewritten": rewritten,
                "duration_ms": _elapsed(started),
            }
        ],
    }


# ---- generate ----


def generate_node(state: dict, deps: GraphDeps) -> dict:
    started = time.perf_counter()
    query = state.get("rewritten_query") or state.get("query", "")
    chunks = _fit_chunks(
        state.get("chunks") or [], deps.config.budget.retrieved_chunks_tokens
    )

    if _over_budget(state, deps):
        return {
            **_cap_payload("generate"),
            "answer": "",
            "claims": [],
            "trace": [
                {
                    "node": "generate",
                    "claims": 0,
                    "reason": "llm_call_cap",
                    "duration_ms": _elapsed(started),
                }
            ],
        }

    call_started = time.perf_counter()
    response = deps.llm.complete(
        system=GENERATE_SYSTEM,
        prompt=generate_prompt(query, chunks, context=state.get("context") or None),
        model=deps.settings.generation_model,
        temperature=0.2,
        max_tokens=deps.config.guardrails.max_answer_tokens,
        json_mode=True,
    )
    log_llm_call(
        logger, node="generate", response=response,
        duration_ms=(time.perf_counter() - call_started) * 1000,
    )
    generated = parse_generation(response.text)
    answer = _scrub(generated.answer, deps)
    claims = [
        {"text": claim.text, "sources": claim.sources} for claim in generated.claims
    ]

    return {
        "answer": answer,
        "claims": claims,
        "llm_calls": int(state.get("llm_calls", 0)) + 1,
        "usage": _merge_usage(state, response),
        "trace": [
            {
                "node": "generate",
                "claims": len(claims),
                "answer_chars": len(answer),
                "duration_ms": _elapsed(started),
            }
        ],
    }


# ---- ground check ----


def ground_check_node(state: dict, deps: GraphDeps) -> dict:
    started = time.perf_counter()
    answer = state.get("answer") or ""
    chunks = state.get("chunks") or []
    claims = list(state.get("claims") or [])

    if not claims and answer.strip():
        # No structured claims: verify the answer as a whole rather than trusting it.
        claims = [{"text": answer, "sources": []}]

    result = verify_claims(
        claims, chunks, min_overlap=deps.config.graph.ground_check_min_overlap
    )
    stripped = _scrub(strip_unsupported(answer, result), deps)

    supported_claims = [
        {"text": verdict.claim, "sources": verdict.matched_chunk_ids}
        for verdict in result.verdicts
        if verdict.supported
    ]
    answered = result.supported_count > 0 and bool(stripped.strip())
    status = "answered" if answered else "honest_failure"

    update: dict[str, Any] = {
        "ground_check": {
            "supported_count": result.supported_count,
            "unsupported_count": result.unsupported_count,
            "verdicts": [asdict(verdict) for verdict in result.verdicts],
        },
        "claims": supported_claims,
        "answer": stripped,
        "status": status,
        "trace": [
            {
                "node": "ground_check",
                "supported": result.supported_count,
                "unsupported": result.unsupported_count,
                "status": status,
                "duration_ms": _elapsed(started),
            }
        ],
    }
    if not answered and not state.get("error"):
        update["error"] = "ungrounded_answer"
    return update


# ---- honest failure ----


def honest_failure_node(state: dict, deps: GraphDeps) -> dict:
    started = time.perf_counter()
    query = state.get("original_query") or state.get("query", "")
    filters = state.get("filters") or {}
    rewrite_count = int(state.get("rewrite_count", 0))

    parts = [f'I didn\'t find a matching past incident for: "{query}".']
    if filters:
        rendered = ", ".join(f"{key}={value}" for key, value in filters.items())
        parts.append(f"Filters applied: {rendered}.")

    if state.get("route") == "entity":
        parts.append("I checked the structured dependency graph.")
    else:
        parts.append(
            "I searched the incident corpus (postmortems, runbooks, architecture docs and "
            "alerts) with hybrid dense + BM25 retrieval."
        )

    if rewrite_count:
        noun = "query" if rewrite_count == 1 else "queries"
        parts.append(f"I tried {rewrite_count} rewritten {noun} before stopping.")

    reason = _failure_reason(state)
    if reason:
        parts.append(f"Reason: {reason}.")

    answer = _scrub(" ".join(parts), deps)
    return {
        "answer": answer,
        "status": "honest_failure",
        "trace": [
            {
                "node": "honest_failure",
                "reason": reason or "",
                "rewrites": rewrite_count,
                "duration_ms": _elapsed(started),
            }
        ],
    }


# ---- helpers ----


def _failure_reason(state: dict) -> str:
    error = state.get("error")
    if error == "llm_call_cap":
        return "the per-turn LLM call budget was exhausted"
    if error == "ungrounded_answer":
        return "no claim in the drafted answer could be traced to retrieved evidence"
    if error and error.startswith("retrieval_failed"):
        return "retrieval failed before usable evidence was found"
    grade = state.get("grade") or {}
    return str(grade.get("reason") or "")


def _over_budget(state: dict, deps: GraphDeps) -> bool:
    return int(state.get("llm_calls", 0)) >= deps.config.guardrails.max_llm_calls_per_turn


def _cap_payload(node: str) -> dict:
    return {"error": "llm_call_cap"}


def _merge_usage(state: dict, response: LLMResponse) -> dict:
    usage = dict(state.get("usage") or {})
    usage["prompt_tokens"] = usage.get("prompt_tokens", 0) + response.prompt_tokens
    usage["completion_tokens"] = (
        usage.get("completion_tokens", 0) + response.completion_tokens
    )
    usage["total_tokens"] = usage.get("total_tokens", 0) + response.total_tokens
    return usage


def _scrub(text: str, deps: GraphDeps) -> str:
    return scrub_secrets(text) if deps.config.guardrails.secret_scrub else (text or "")


def _serialize_chunk(chunk: Any) -> dict:
    trail = chunk.score_trail()
    trail["text"] = chunk.text
    trail["metadata"] = chunk.metadata
    trail["score"] = chunk.score
    return trail


def _fit_chunks(chunks: list[dict], token_cap: int) -> list[dict]:
    kept: list[dict] = []
    used = 0
    for chunk in chunks:
        size = count_tokens(chunk.get("text") or "")
        if kept and used + size > token_cap:
            break
        kept.append(chunk)
        used += size
    return kept


def _elapsed(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)
