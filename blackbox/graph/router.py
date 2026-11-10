"""Routing: structured entity lookup, direct meta answers, or retrieval.

The router is deliberately deterministic. A misrouted incident question is expensive (it
loses retrieval entirely), so the safe default is retrieval; entity and direct paths are
only taken when the query shape is unambiguous. This also keeps the router free.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

_GREETING_RE = re.compile(
    r"^(hi|hello|hey|yo|good morning|good afternoon|good evening|thanks|thank you)[!. ]*$",
    re.IGNORECASE,
)
_CAPABILITY_RE = re.compile(
    r"\b(what can you do|who are you|what are you|your capabilities|capabilities|"
    r"what do you do|how can you help)\b",
    re.IGNORECASE,
)
_DEPENDENCY_RE = re.compile(
    r"\b(depend(?:s|ing)?\s+on|depends\s+upon|dependenc(?:y|ies)|who\s+calls)\b",
    re.IGNORECASE,
)

_DIRECT_GREETING = (
    "Hi. I'm Blackbox, an on-call incident assistant. Ask me about past incidents, "
    "runbooks, architecture docs or alert definitions."
)
_DIRECT_CAPABILITY = (
    "I search past incident postmortems, runbooks, architecture docs and alert definitions "
    "with hybrid retrieval, then answer only from what the retrieved evidence supports. I "
    "can also answer dependency questions from the structured service graph."
)


@dataclass(slots=True)
class RouteDecision:
    route: str
    reason: str
    service: str | None = None


def classify_route(
    query: str,
    entity_store: Any | None = None,
    *,
    direct_answers: bool = True,
) -> RouteDecision:
    text = (query or "").strip()
    if not text:
        return RouteDecision("direct", "empty query")

    if direct_answers and _GREETING_RE.match(text):
        return RouteDecision("direct", "greeting")
    if direct_answers and _CAPABILITY_RE.search(text):
        return RouteDecision("direct", "capability question")

    if _DEPENDENCY_RE.search(text) and entity_store is not None:
        service = find_service(text, entity_store)
        if service:
            return RouteDecision("entity", f"dependency question about {service}", service)
        return RouteDecision("retrieval", "dependency question but unknown service")

    return RouteDecision("retrieval", "incident question")


def find_service(query: str, entity_store: Any) -> str | None:
    """Longest known service name mentioned in the query."""
    lowered = (query or "").lower()
    try:
        services = entity_store.get_services()
    except Exception:  # noqa: BLE001 - an unavailable store must not break routing
        return None
    matches = [service for service in services if service.lower() in lowered]
    return max(matches, key=len) if matches else None


def router_node(
    state: dict,
    *,
    entity_store: Any | None = None,
    direct_answers: bool = True,
) -> dict:
    started = time.perf_counter()
    decision = classify_route(
        state.get("query", ""), entity_store, direct_answers=direct_answers
    )
    return {
        "route": decision.route,
        "route_reason": decision.reason,
        "trace": [
            {
                "node": "router",
                "route": decision.route,
                "reason": decision.reason,
                "duration_ms": _elapsed(started),
            }
        ],
    }


def entity_lookup_node(state: dict, *, entity_store: Any | None = None) -> dict:
    """Answer a dependency question from the structured graph, as evidence chunks.

    Emitting regular evidence keeps the rest of the graph uniform: the same grading,
    generation and ground check run, and the trace shows where the evidence came from.
    """
    started = time.perf_counter()
    query = state.get("query", "")
    service = find_service(query, entity_store) if entity_store is not None else None

    chunks: list[dict] = []
    if service:
        dependents = entity_store.services_depending_on(service)
        if dependents:
            text = (
                f"{', '.join(dependents)} depend on {service} according to the dependency "
                "graph."
            )
            if len(dependents) == 1:
                text = f"{dependents[0]} depends on {service} according to the dependency graph."
        else:
            text = f"No services in the dependency graph depend on {service}."
        chunks.append(
            {
                "chunk_id": f"graph::dependents::{service}",
                "doc_id": "graph:dependencies",
                "section": "Dependencies",
                "doc_type": "graph",
                "text": text,
                "metadata": {"services": [service, *dependents], "section": "Dependencies"},
                "fused_rank": 1,
                "score": 1.0,
            }
        )

    return {
        "chunks": chunks,
        "trace": [
            {
                "node": "entity_lookup",
                "service": service,
                "evidence": len(chunks),
                "duration_ms": _elapsed(started),
            }
        ],
    }


def direct_answer_node(state: dict) -> dict:
    started = time.perf_counter()
    query = state.get("query", "")
    if _GREETING_RE.match((query or "").strip()):
        answer = _DIRECT_GREETING
    else:
        answer = _DIRECT_CAPABILITY
    return {
        "answer": answer,
        "status": "direct",
        "claims": [],
        "trace": [
            {
                "node": "direct_answer",
                "status": "direct",
                "duration_ms": _elapsed(started),
            }
        ],
    }


def _elapsed(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)
