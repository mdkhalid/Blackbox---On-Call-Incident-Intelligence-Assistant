"""Graph assembly and execution helpers.

Node-level streaming exists from the start rather than being retrofitted: the API streams
these same events in Phase 5, and the trace viewer renders them in Phase 6.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from functools import partial
from typing import Any

from langgraph.graph import END, START, StateGraph

from blackbox.config.settings import AppConfig, Settings, get_config, get_settings
from blackbox.graph.nodes import (
    GraphDeps,
    generate_node,
    grade_node,
    ground_check_node,
    honest_failure_node,
    retrieve_node,
    rewrite_node,
)
from blackbox.graph.router import direct_answer_node, entity_lookup_node, router_node
from blackbox.graph.state import GraphState
from blackbox.llm import FakeLLMProvider, build_llm_provider
from blackbox.logging_setup import get_logger

logger = get_logger(__name__)


def build_graph(
    retriever: Any,
    *,
    llm: Any | None = None,
    entity_store: Any | None = None,
    settings: Settings | None = None,
    config: AppConfig | None = None,
    checkpointer: Any | None = None,
):
    settings = settings or get_settings()
    config = config or get_config()
    if llm is None:
        llm = _resolve_llm(settings)

    deps = GraphDeps(
        retriever=retriever,
        llm=llm,
        config=config,
        settings=settings,
        entity_store=entity_store,
    )
    rewrite_cap = config.retrieval.rewrite_cap

    builder = StateGraph(GraphState)
    builder.add_node(
        "router",
        partial(
            router_node,
            entity_store=entity_store,
            direct_answers=config.graph.direct_answers,
        ),
    )
    builder.add_node(
        "entity_lookup", partial(entity_lookup_node, entity_store=entity_store)
    )
    builder.add_node("direct_answer", direct_answer_node)
    builder.add_node("retrieve", partial(retrieve_node, deps=deps))
    builder.add_node("grade", partial(grade_node, deps=deps))
    builder.add_node("rewrite", partial(rewrite_node, deps=deps))
    builder.add_node("generate", partial(generate_node, deps=deps))
    builder.add_node("ground_check", partial(ground_check_node, deps=deps))
    builder.add_node("honest_failure", partial(honest_failure_node, deps=deps))

    builder.add_edge(START, "router")
    builder.add_conditional_edges(
        "router",
        _route_selector,
        {
            "entity": "entity_lookup",
            "direct": "direct_answer",
            "retrieval": "retrieve",
        },
    )
    builder.add_conditional_edges(
        "entity_lookup",
        _entity_selector,
        {"generate": "generate", "honest_failure": "honest_failure"},
    )
    builder.add_edge("direct_answer", END)
    builder.add_edge("retrieve", "grade")
    builder.add_conditional_edges(
        "grade",
        partial(_grade_selector, cap=rewrite_cap),
        {"generate": "generate", "rewrite": "rewrite", "honest_failure": "honest_failure"},
    )
    builder.add_edge("rewrite", "retrieve")
    builder.add_edge("generate", "ground_check")
    builder.add_conditional_edges(
        "ground_check",
        _ground_selector,
        {"answer": END, "honest_failure": "honest_failure"},
    )
    builder.add_edge("honest_failure", END)

    return builder.compile(checkpointer=checkpointer)


async def run_graph(graph, state: GraphState) -> dict:
    """Run the graph to completion.

    The nodes are async, so the graph is invoked asynchronously: no worker thread is
    held for the duration of a hosted LLM round trip.
    """
    return dict(await graph.ainvoke(dict(state)))


async def stream_graph_events(graph, state: GraphState) -> AsyncIterator[dict]:
    """Yield one event per node update, then a final event with the merged state."""
    merged: dict[str, Any] = dict(state)
    async for update in graph.astream(dict(state), stream_mode="updates"):
        for node, values in update.items():
            if not isinstance(values, dict):
                continue
            for key, value in values.items():
                if key == "trace" and isinstance(value, list):
                    merged.setdefault("trace", [])
                    merged["trace"].extend(value)
                else:
                    merged[key] = value
            yield {"event": "node", "node": node, "data": values}
    yield {"event": "final", "data": merged}


def _resolve_llm(settings: Settings):
    try:
        return build_llm_provider(settings)
    except ValueError:
        if settings.env == "prod":
            raise
        logger.warning("llm_provider_unavailable", fallback="fake")
        return FakeLLMProvider()


def _route_selector(state: dict) -> str:
    return state.get("route") or "retrieval"


def _entity_selector(state: dict) -> str:
    return "generate" if state.get("chunks") else "honest_failure"


def _grade_selector(state: dict, *, cap: int) -> str:
    grade = state.get("grade") or {}
    if grade.get("sufficient"):
        return "generate"
    if state.get("error") == "llm_call_cap":
        return "honest_failure"
    if int(state.get("rewrite_count", 0)) < cap:
        return "rewrite"
    return "honest_failure"


def _ground_selector(state: dict) -> str:
    return "answer" if state.get("status") == "answered" else "honest_failure"
