"""Per-node LLM cost derivation."""
from __future__ import annotations

from blackbox.config.settings import CostConfig, ModelPrice
from blackbox.observability.cost import conversation_cost, entry_cost, turn_cost

PRICES = CostConfig(
    currency="USD",
    models={
        "gpt-4o-mini": ModelPrice(input_per_1m=0.15, output_per_1m=0.60),
        "gpt-4o": ModelPrice(input_per_1m=2.50, output_per_1m=10.00),
    },
)


def _entry(node, model, prompt, completion):
    return {
        "node": node,
        "model": model,
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
    }


def test_entry_cost_prices_input_and_output():
    cost, unpriced = entry_cost(
        _entry("grade", "gpt-4o-mini", 1_000_000, 0), PRICES.models
    )
    assert cost == 0.15
    assert unpriced is None


def test_entry_cost_flags_unknown_model():
    cost, unpriced = entry_cost(_entry("grade", "mystery", 100, 100), PRICES.models)
    assert cost == 0.0
    assert unpriced == "mystery"


def test_turn_cost_breaks_down_by_node():
    trace = [
        _entry("grade", "gpt-4o-mini", 1000, 100),
        _entry("rewrite", "gpt-4o-mini", 500, 50),
        _entry("generate", "gpt-4o", 2000, 500),
    ]
    report = turn_cost(trace, PRICES)

    assert report["currency"] == "USD"
    assert report["unpriced_models"] == []
    assert set(report["by_node"]) == {"grade", "rewrite", "generate"}
    assert report["total_usd"] == round(sum(report["by_node"].values()), 6)


def test_turn_cost_reports_unpriced_models():
    report = turn_cost([_entry("grade", "mystery", 10, 10)], PRICES)
    assert report["unpriced_models"] == ["mystery"]
    assert report["total_usd"] == 0.0


def test_disabled_cost_returns_zero():
    config = CostConfig(enabled=False, models=PRICES.models)
    report = turn_cost([_entry("grade", "gpt-4o", 1_000_000, 0)], config)
    assert report["enabled"] is False
    assert report["total_usd"] == 0.0


def test_entries_without_usage_are_ignored():
    report = turn_cost([{"node": "retrieve", "duration_ms": 5.0}], PRICES)
    assert report["total_usd"] == 0.0
    assert report["unpriced_models"] == []


def test_conversation_cost_sums_turns():
    trace = [_entry("generate", "gpt-4o", 1_000_000, 0)]
    report = conversation_cost([trace, trace], PRICES)
    assert report["turns"] == 2
    assert report["total_usd"] == 5.0
