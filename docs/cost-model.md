# Cost Model

Blackbox reports LLM cost per conversation and per tenant. Cost is derived from the
tokens the graph already records, priced from configuration.

## How it works

- Each LLM node (`grade`, `rewrite`, `generate`) appends `model` and
  `usage.{prompt_tokens,completion_tokens}` to its trace entry.
- `blackbox/observability/cost.py` prices a trace entry from `cost.models` in
  `config.yaml` (US$ per million tokens) and aggregates by node and by conversation.
- Cost is computed **on read**, not stored. Repricing a model immediately reprices
  historical turns, and adding a model needs no migration.
- A model with no configured price is reported in `unpriced_models`. It is never
  counted as free, because a report that silently omits a model is worse than one that
  says it cannot price it.

## Endpoints

| Endpoint | Returns |
|---|---|
| `GET /api/conversations/{id}/cost` | `turns`, `total_usd`, `by_node`, `unpriced_models`, `currency` |
| `GET /api/admin/cost?days=30` | The same aggregate across the tenant's turns in the window |

Both read the same function, so the per-conversation and tenant numbers cannot
disagree.

## Configuration

```yaml
cost:
  enabled: true
  currency: USD
  models:
    fake: {input_per_1m: 0.0, output_per_1m: 0.0}
    gpt-4o-mini: {input_per_1m: 0.15, output_per_1m: 0.60}
    gpt-4o: {input_per_1m: 2.50, output_per_1m: 10.00}
```

`cost.enabled: false` returns zero-cost reports with `enabled: false`, which is the
honest answer when pricing is intentionally off.

## Model tiering

The graph already defaults to the cheap model for grading and rewriting
(`GRADING_MODEL`, `REWRITE_MODEL`, default `gpt-4o-mini`) and the stronger model for
generation (`GENERATION_MODEL`). The cost report shows the effect directly: on a
grounded turn the `generate` node dominates, so the tiering that matters is generation.

## Budgets and quotas

Daily per-tenant limits live in `blackbox/api/quotas.py` and are enforced at chat
admission:

- `TENANT_QUOTA_TOKENS_PER_DAY`, `TENANT_QUOTA_LLM_CALLS_PER_DAY` — token and call
  ceilings.
- `TENANT_BUDGET_USD_PER_DAY` — a dollar budget. The turn's cost is computed with the
  same `turn_cost` function, recorded against `bb:quota:cost:{tenant}:{day}`, and checked
  at admission; exceeding it rejects with the reason `budget_usd`. `0` disables it.
- Redis-backed when reachable, with a local fallback; all of them fail open, matching
  rate limiting, so a Redis outage does not stop paid traffic.

## What the numbers mean

The offline provider (`fake`, or the deterministic fallback) prices at zero. A hosted
run is the only one that produces a non-zero report; the figures in a report should
name the models that produced them, which the `by_node` breakdown does.
