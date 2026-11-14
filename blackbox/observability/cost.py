"""Per-node LLM cost, derived from the token usage stored in a turn's trace.

Cost is computed on read rather than stored: the trace already records the model and
the prompt/completion tokens for every LLM node, so pricing is a pure function of
stored data. That means historical turns are repriced when a price changes, and no
migration is needed to add or change a price.

A model with no configured price is reported in `unpriced_models`, never silently
counted as free: a cost report that quietly omits a model is worse than one that says
it cannot price it.
"""
from __future__ import annotations

from typing import Any

from blackbox.config.settings import CostConfig, ModelPrice

PER_MILLION = 1_000_000.0


def entry_cost(entry: dict[str, Any], models: dict[str, ModelPrice]) -> tuple[float, str | None]:
    """The cost of one trace entry, and the unpriced model name if it could not be priced."""
    usage = entry.get("usage")
    if not isinstance(usage, dict):
        return 0.0, None
    model = str(entry.get("model") or "")
    price = models.get(model)
    if price is None:
        return 0.0, model or "unknown"
    prompt = float(usage.get("prompt_tokens") or 0)
    completion = float(usage.get("completion_tokens") or 0)
    cost = (
        prompt / PER_MILLION * price.input_per_1m
        + completion / PER_MILLION * price.output_per_1m
    )
    return cost, None


def turn_cost(trace: list[dict[str, Any]], config: CostConfig) -> dict[str, Any]:
    """Cost of one turn, broken down by graph node."""
    if not config.enabled:
        return _empty(config)

    by_node: dict[str, float] = {}
    unpriced: set[str] = set()
    for entry in trace or []:
        if not isinstance(entry, dict):
            continue
        cost, unpriced_model = entry_cost(entry, config.models)
        if unpriced_model is not None:
            unpriced.add(unpriced_model)
            continue
        node = str(entry.get("node") or "unknown")
        by_node[node] = by_node.get(node, 0.0) + cost

    return {
        "enabled": True,
        "currency": config.currency,
        "total_usd": round(sum(by_node.values()), 6),
        "by_node": {node: round(value, 6) for node, value in sorted(by_node.items())},
        "unpriced_models": sorted(unpriced),
    }


def conversation_cost(
    traces: list[list[dict[str, Any]]], config: CostConfig
) -> dict[str, Any]:
    """Aggregate the cost of every turn in a conversation."""
    if not config.enabled:
        return {**_empty(config), "turns": len(traces)}

    by_node: dict[str, float] = {}
    unpriced: set[str] = set()
    total = 0.0
    for trace in traces:
        result = turn_cost(trace, config)
        total += float(result["total_usd"])
        unpriced.update(result["unpriced_models"])
        for node, value in result["by_node"].items():
            by_node[node] = by_node.get(node, 0.0) + float(value)

    return {
        "enabled": True,
        "currency": config.currency,
        "turns": len(traces),
        "total_usd": round(total, 6),
        "by_node": {node: round(value, 6) for node, value in sorted(by_node.items())},
        "unpriced_models": sorted(unpriced),
    }


def _empty(config: CostConfig) -> dict[str, Any]:
    return {
        "enabled": False,
        "currency": config.currency,
        "total_usd": 0.0,
        "by_node": {},
        "unpriced_models": [],
    }
