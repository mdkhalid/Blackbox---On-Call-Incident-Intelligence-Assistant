"""Routing: structured entity lookup, direct meta answers, or retrieval.

The router is deliberately deterministic. A misrouted incident question is expensive (it
loses retrieval entirely), so the safe default is retrieval; entity and direct paths are
only taken when the query shape is unambiguous. This also keeps the router free.
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Any

from blackbox.graph.state import GraphState

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

# Definitional questions need no retrieval. These are fixed, safe definitions rather than
# model output, so routing them direct cannot introduce an ungrounded claim.
_GLOSSARY = {
    "p99": "p99 is the 99th-percentile latency: 99% of requests complete faster than it. "
    "It surfaces tail latency, which averages hide.",
    "p95": "p95 is the 95th-percentile latency: 95% of requests complete faster than it.",
    "p50": "p50 is the median latency: half of requests complete faster than it.",
    "slo": "An SLO is a target reliability level for a service, e.g. 99.9% of requests "
    "under 200 ms.",
    "sla": "An SLA is a contractual service-level agreement with a customer. It is "
    "external; an SLO is the internal reliability target.",
    "sev1": "SEV1 is the highest incident severity: broad customer impact requiring an "
    "immediate, all-hands response.",
    "sev2": "SEV2 is a high-severity incident with significant but partial impact, below "
    "SEV1.",
    "sev3": "SEV3 is a moderate-severity incident with limited customer impact.",
    "runbook": "A runbook is a documented operational procedure for diagnosing and "
    "mitigating a known failure mode.",
    "postmortem": "A postmortem is a blameless review of an incident's timeline, root "
    "cause, remediation and action items.",
    "rca": "Root cause analysis (RCA) identifies the underlying cause of an incident, as "
    "opposed to its immediate trigger.",
    "error budget": "An error budget is the allowed failure implied by an SLO: for a "
    "99.9% SLO it is 0.1% of requests.",
}

_DEFINITION_PATTERNS = [
    re.compile(
        r"^\s*what\s+does\s+(?P<term>[a-z0-9][\w\s\-]{0,40}?)\s+mean\s*\??\s*$", re.IGNORECASE
    ),
    re.compile(
        r"^\s*what\s+(?:is|are)\s+(?:an?\s+|the\s+)?(?P<term>[a-z0-9][\w\s\-]{0,40}?)\s*\??\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^\s*(?:define|meaning\s+of)\s+(?:an?\s+|the\s+)?(?P<term>[a-z0-9][\w\s\-]{0,40}?)\s*\??\s*$",
        re.IGNORECASE,
    ),
]


def definition_term(text: str) -> str | None:
    """Return a glossary term when the query is a definitional question."""
    for pattern in _DEFINITION_PATTERNS:
        match = pattern.match((text or "").strip())
        if not match:
            continue
        term = " ".join(match.group("term").lower().split()).strip(" ?.")
        if term in _GLOSSARY:
            return term
    return None


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
    if direct_answers and definition_term(text):
        return RouteDecision("direct", "definition question")
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


async def router_node(
    state: GraphState,
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


async def entity_lookup_node(
    state: GraphState, *, entity_store: Any | None = None
) -> dict:
    """Answer a dependency question from the structured graph, as evidence chunks.

    Emitting regular evidence keeps the rest of the graph uniform: the same grading,
    generation and ground check run, and the trace shows where the evidence came from.
    """
    started = time.perf_counter()
    query = state.get("query", "")
    service = None
    dependents: list[str] = []
    if entity_store is not None:
        # The entity store is a blocking SQLAlchemy repository, so its calls are
        # pushed to a worker thread rather than run on the event loop.
        service = await asyncio.to_thread(find_service, query, entity_store)
        if service:
            dependents = await asyncio.to_thread(
                entity_store.services_depending_on, service
            )

    chunks: list[dict] = []
    if service:
        text = (
            f"{', '.join(dependents)} depend on {service} according to the dependency "
            "graph."
        )
        if len(dependents) == 1:
            text = f"{dependents[0]} depends on {service} according to the dependency graph."
        elif not dependents:
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


async def direct_answer_node(state: GraphState) -> dict:
    started = time.perf_counter()
    query = state.get("query", "")
    term = definition_term(query)
    if term:
        answer = f"{term.upper()}: {_GLOSSARY[term]}"
    elif _GREETING_RE.match((query or "").strip()):
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
