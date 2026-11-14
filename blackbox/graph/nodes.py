"""Graph nodes: retrieve, grade, rewrite, generate, ground check, honest failure.

Nodes are thin and observable: each one returns a partial state update plus a trace event,
so the same run can be replayed by the trace viewer and streamed by the API.
"""
from __future__ import annotations

import asyncio
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
    evidence_lines,
    generate_prompt,
    grade_prompt,
    rewrite_prompt,
)
from blackbox.graph.state import GraphState
from blackbox.llm.base import LLMProvider, LLMResponse
from blackbox.logging_setup import get_logger
from blackbox.memory.tokens import count_tokens
from blackbox.observability.callbacks import log_llm_call
from blackbox.observability.metrics import record_llm_error, record_retrieval
from blackbox.observability.tracing import span
from blackbox.retrieval.models import RetrievalResult

logger = get_logger(__name__)


@dataclass
class GraphDeps:
    retriever: Any
    llm: LLMProvider
    config: AppConfig
    settings: Settings
    entity_store: Any | None = None


# ---- retrieve ----


async def retrieve_node(state: GraphState, deps: GraphDeps) -> dict:
    started = time.perf_counter()
    query = state.get("rewritten_query") or state.get("query", "")
    filters = state.get("filters") or None

    chunks: list[dict] = []
    timings: dict[str, float] = dict(state.get("timings") or {})
    error = state.get("error")
    try:
        with span(
            "retrieval.retrieve",
            **{
                "retrieval.top_k": deps.config.retrieval.top_k,
                "retrieval.tenant_id": state.get("tenant_id"),
            },
        ) as retrieval_span:
            result = await _aretrieve(
                deps.retriever, query, top_k=deps.config.retrieval.top_k, filter=filters,
                tenant_id=state.get("tenant_id"),
            )
            retrieval_span.set_attribute("retrieval.chunks", len(result.chunks))
            retrieval_span.set_attribute(
                "retrieval.cached", "cache_ms" in result.timings
            )
            if "total_ms" in result.timings:
                retrieval_span.set_attribute(
                    "retrieval.total_ms", float(result.timings["total_ms"])
                )
        chunks = [_serialize_chunk(hit) for hit in result.chunks]
        timings.update(result.timings)
        record_retrieval(result.timings)
    except Exception as exc:  # noqa: BLE001 - a store failure must become an honest answer
        error = f"retrieval_failed:{type(exc).__name__}"
        logger.warning("retrieve_node_failed", query=query[:120], error=error)

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


async def grade_node(state: GraphState, deps: GraphDeps) -> dict:
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
    response = await _acomplete(
        deps.llm,
        node="grade",
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
                **_trace_usage(response, deps.settings.grading_model),
            }
        ],
    }


# ---- rewrite ----


async def rewrite_node(state: GraphState, deps: GraphDeps) -> dict:
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
    response = await _acomplete(
        deps.llm,
        node="rewrite",
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
                **_trace_usage(response, deps.settings.rewrite_model),
            }
        ],
    }


# ---- generate ----


async def generate_node(state: GraphState, deps: GraphDeps) -> dict:
    started = time.perf_counter()
    query = state.get("rewritten_query") or state.get("query", "")
    context = state.get("context") or ""
    report = dict(state.get("budget") or {})
    all_chunks = list(state.get("chunks") or [])

    # The chunk room is the smaller of the configured chunk allocation and whatever the
    # memory side left of the input ceiling. Measuring the prompt that is actually
    # built - rather than the retrieval result - is what makes the reported budget
    # match the request the provider received.
    ceiling = max(
        0,
        deps.config.budget.total_cap_tokens - deps.config.budget.output_headroom_tokens,
    )
    allowance = min(
        deps.config.budget.retrieved_chunks_tokens,
        int(report.get("chunk_allowance", deps.config.budget.retrieved_chunks_tokens)),
    )

    # The prompt scaffolding (task header, context and evidence labels, the JSON
    # instruction) is part of the request, so it is measured before the evidence is
    # added rather than discovered afterwards.
    base_tokens = count_tokens(
        GENERATE_SYSTEM + generate_prompt(query, [], context=context or None)
    )
    allowance = max(0, min(allowance, ceiling - base_tokens))

    chunks = _fit_chunks(all_chunks, allowance)
    prompt = generate_prompt(query, chunks, context=context or None)
    prompt_tokens = count_tokens(GENERATE_SYSTEM + prompt)

    evictions: list[dict] = []
    while prompt_tokens > ceiling and chunks:
        chunks.pop()
        evictions.append({"bucket": "retrieved_chunks", "reason": "prompt_cap"})
        prompt = generate_prompt(query, chunks, context=context or None)
        prompt_tokens = count_tokens(GENERATE_SYSTEM + prompt)

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
    response = await _acomplete(
        deps.llm,
        node="generate",
        system=GENERATE_SYSTEM,
        prompt=prompt,
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
        "budget": _budget_report(
            report,
            chunks=chunks,
            all_chunks=all_chunks,
            context=context,
            query=query,
            prompt_tokens=prompt_tokens,
            ceiling=ceiling,
            evictions=evictions,
        ),
        "llm_calls": int(state.get("llm_calls", 0)) + 1,
        "usage": _merge_usage(state, response),
        "trace": [
            {
                "node": "generate",
                "claims": len(claims),
                "answer_chars": len(answer),
                "duration_ms": _elapsed(started),
                **_trace_usage(response, deps.settings.generation_model),
            }
        ],
    }


# ---- ground check ----


async def ground_check_node(state: GraphState, deps: GraphDeps) -> dict:
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


async def honest_failure_node(state: GraphState, deps: GraphDeps) -> dict:
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


def _failure_reason(state: GraphState) -> str:
    error = state.get("error")
    if error == "llm_call_cap":
        return "the per-turn LLM call budget was exhausted"
    if error == "ungrounded_answer":
        return "no claim in the drafted answer could be traced to retrieved evidence"
    if error and error.startswith("retrieval_failed"):
        return "retrieval failed before usable evidence was found"
    grade = state.get("grade") or {}
    return str(grade.get("reason") or "")


def _over_budget(state: GraphState, deps: GraphDeps) -> bool:
    return int(state.get("llm_calls", 0)) >= deps.config.guardrails.max_llm_calls_per_turn


def _cap_payload(node: str) -> dict:
    return {"error": "llm_call_cap"}


def _trace_usage(response: LLMResponse, fallback_model: str) -> dict:
    """Record which model served a node and what it cost in tokens.

    The aggregate `usage` keeps the turn total; this per-node record is what makes a
    cost breakdown by node (grade vs rewrite vs generate) possible from the stored
    trace, without a second accounting path.
    """
    return {
        "model": response.model or fallback_model,
        "usage": {
            "prompt_tokens": response.prompt_tokens,
            "completion_tokens": response.completion_tokens,
        },
    }


def _merge_usage(state: GraphState, response: LLMResponse) -> dict:
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


async def _retrieve(retriever: Any, query: str, *, top_k: int, filter: Any, tenant_id: str | None = None) -> RetrievalResult:
    """Retrieve chunks together with the timings of *this* call.

    `retrieve_detailed` is the primary API because the retriever is a process-wide
    singleton: timings kept on the instance are shared by every concurrent request, so
    one request's trace ends up carrying another's latencies. A retriever that only
    implements `retrieve` still works; it simply contributes no stage timings.
    """
    detailed = getattr(retriever, "retrieve_detailed", None)
    if callable(detailed):
        return detailed(query, top_k=top_k, filter=filter, tenant_id=tenant_id)
    chunks = list(retriever.retrieve(query, top_k=top_k, filter=filter, tenant_id=tenant_id))
    return RetrievalResult(chunks=chunks)


async def _aretrieve(
    retriever: Any, query: str, *, top_k: int, filter: Any, tenant_id: str | None = None
) -> RetrievalResult:
    """Async retrieval that keeps the same per-call timings contract as `_retrieve`.

    A retriever that can retrieve asynchronously is awaited directly; one that cannot
    is pushed to a worker thread so the event loop is never blocked by a blocking
    store call.
    """
    detailed = getattr(retriever, "aretrieve_detailed", None)
    if callable(detailed):
        return await detailed(query, top_k=top_k, filter=filter, tenant_id=tenant_id)
    detailed = getattr(retriever, "retrieve_detailed", None)
    if callable(detailed):
        return await asyncio.to_thread(detailed, query, top_k=top_k, filter=filter, tenant_id=tenant_id)
    chunks = await asyncio.to_thread(
        retriever.retrieve, query, top_k=top_k, filter=filter, tenant_id=tenant_id
    )
    return RetrievalResult(chunks=list(chunks))


async def _acomplete(llm: Any, *, node: str, **kwargs: Any) -> LLMResponse:
    """Call the provider's async entry point, falling back to a worker thread.

    A provider that only implements the synchronous `complete` still works, but it
    occupies a worker thread for the duration of the call. A failure after retries
    is counted against the node that asked, then re-raised: the graph's existing
    error path still decides what the user sees.
    """
    acomplete = getattr(llm, "acomplete", None)
    with span(
        "llm.complete",
        **{
            "llm.node": node,
            "llm.model": kwargs.get("model"),
            "llm.max_tokens": kwargs.get("max_tokens"),
            "llm.json_mode": kwargs.get("json_mode"),
        },
    ) as current:
        try:
            if callable(acomplete):
                response = await acomplete(**kwargs)
            else:
                response = await asyncio.to_thread(llm.complete, **kwargs)
        except Exception as exc:
            current.record_exception(exc)
            record_llm_error(node)
            raise
        current.set_attribute("llm.prompt_tokens", response.prompt_tokens)
        current.set_attribute("llm.completion_tokens", response.completion_tokens)
        current.set_attribute("llm.total_tokens", response.total_tokens)
        current.set_attribute("llm.cached", response.cached)
        return response


def _fit_chunks(chunks: list[dict], token_cap: int) -> list[dict]:
    """Fit chunks into the token cap, measuring the evidence as it is rendered.

    The rendered evidence block is what the provider receives, so it is what the cap
    has to be measured against. A chunk that does not fit on its own is dropped rather
    than truncated: half an incident section is worse than no evidence at all.
    """
    kept: list[dict] = []
    used = 0
    for chunk in chunks:
        size = count_tokens(evidence_lines([chunk]))
        if used + size > token_cap:
            break
        kept.append(chunk)
        used += size
    return kept


def _budget_report(
    report: dict,
    *,
    chunks: list[dict],
    all_chunks: list[dict],
    context: str,
    query: str,
    prompt_tokens: int,
    ceiling: int,
    evictions: list[dict],
) -> dict:
    """Report the allocation of the request that was actually sent.

    The numbers here are measured on the real strings: the rendered context, the
    rendered evidence and the prompt handed to the provider. A budget report that
    estimates a different request than the one that was sent is worse than no report
    at all, because it gives false confidence that the context window is respected.
    """
    buckets = {name: dict(values) for name, values in (report.get("buckets") or {}).items()}
    sent_evidence = count_tokens(evidence_lines(chunks))
    requested_evidence = count_tokens(evidence_lines(all_chunks))
    buckets["retrieved_chunks"] = {
        "requested": requested_evidence,
        "allocated": sent_evidence,
    }

    content_total = (
        count_tokens(GENERATE_SYSTEM)
        + count_tokens(context)
        + sent_evidence
        + count_tokens(query)
    )

    merged = dict(report)
    merged["buckets"] = buckets
    merged["total_tokens"] = content_total
    merged["prompt_tokens"] = prompt_tokens
    merged["input_cap"] = ceiling
    merged["chunk_allowance"] = max(0, ceiling - content_total)
    merged["over_cap"] = prompt_tokens > ceiling
    if evictions:
        merged["evictions"] = [*(report.get("evictions") or []), *evictions]
    if sent_evidence < requested_evidence:
        merged.setdefault("evictions", []).append(
            {"bucket": "retrieved_chunks", "reason": "generation_fit"}
        )
    return merged


def _elapsed(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)
