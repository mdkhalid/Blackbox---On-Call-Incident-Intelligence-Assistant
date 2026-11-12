"""Typed graph state.

Every field is JSON-serialisable rather than a domain object, because the same state is
streamed to the API (Phase 5), persisted per turn, and rendered by the trace viewer. The
score trail from retrieval survives the trip: chunks keep each stage's score and rank.
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class GraphState(TypedDict, total=False):
    query: str
    original_query: str
    rewritten_query: str
    filters: dict[str, Any]
    tenant_id: str

    route: str
    route_reason: str

    chunks: list[dict[str, Any]]
    grade: dict[str, Any]
    grade_history: list[dict[str, Any]]
    rewrite_count: int

    answer: str
    claims: list[dict[str, Any]]
    ground_check: dict[str, Any]

    status: str
    error: str | None

    # Phase 4: budgeted memory injection, assembled before the turn reaches the graph.
    context: str
    budget: dict[str, Any]
    injection_flags: list[str]

    llm_calls: int
    usage: dict[str, int]
    timings: dict[str, float]

    trace: Annotated[list[dict[str, Any]], operator.add]


def initial_state(
    query: str,
    *,
    filters: dict[str, Any] | None = None,
    original_query: str | None = None,
    context: str | None = None,
    budget: dict[str, Any] | None = None,
    injection_flags: list[str] | None = None,
    tenant_id: str | None = None,
) -> GraphState:
    from blackbox.ingestion.models import DEFAULT_TENANT

    resolved = tenant_id if tenant_id else DEFAULT_TENANT
    if not resolved:
        raise ValueError("tenant_id must not be empty")
    return GraphState(
        query=query,
        original_query=original_query if original_query is not None else query,
        filters=dict(filters or {}),
        tenant_id=str(resolved),
        context=context or "",
        budget=dict(budget or {}),
        injection_flags=list(injection_flags or []),
        chunks=[],
        grade_history=[],
        rewrite_count=0,
        llm_calls=0,
        usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        timings={},
        trace=[],
        error=None,
    )
