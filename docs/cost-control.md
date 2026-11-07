# Cost Control — Blackbox

## Strategy
- **Model tiering**: cheap/fast models for grading, rewrite, summarization, entity extraction; stronger model only for final answer generation.
- **Caching**: LLM response cache (dev) + retrieval/embedding caches (Redis) to avoid repeat cost.
- **Token budgets**: hard cap per request; eviction before overflow.
- **Timeouts**: retrieval and LLM timeouts prevent runaway cost.
- **Rate limiting**: per-user RPS caps.
- **Batching**: embeddings batched at ingest.

## Model Assignment (config-driven)
| Task | Model (default) | Rationale |
|---|---|---|
| Embeddings | text-embedding-3-small | cheap, good quality |
| Generation (final answer) | gpt-4o / claude-sonnet-4 | quality matters |
| Grading | gpt-4o-mini / claude-haiku | binary-ish, cheap |
| Rewrite | gpt-4o-mini / claude-haiku | short output |
| Summary compression | gpt-4o-mini / claude-haiku | frequent |
| Entity extraction | gpt-4o-mini / claude-haiku | structured, cheap |
| Reranker | cross-encoder (local) | free after download |
| Ground check | gpt-4o-mini / claude-haiku | claim verification |

## Caching Layers
1. **LLM response cache (dev)**: LangChain `SQLiteCache` or Redis-backed cache keyed by (model, prompt, params). Disabled in prod or with TTL. Saves money while iterating on graph.
2. **Embedding cache**: Redis, keyed by (model, text hash). Avoids re-embedding on re-ingest of unchanged chunks.
3. **Retrieval cache**: Redis, keyed by (query, filters, top-k). Short TTL (e.g. 60s) for repeated identical questions. Invalidate on ingest.
4. **Rerank cache**: Redis, keyed by (query, chunk_ids).

## Token Budgeting
- Explicit allocation (config, in tokens):
  - system prompt: 800
  - entity memory: 400
  - summary: 600
  - recent turns: 1200
  - retrieved chunks: 2000
  - output headroom: 1024
  - total cap: 6000 (under model context)
- Eviction order when exceeded: oldest recent turns → compress summary → trim lowest-score chunks → truncate entity (last resort).
- Exposed via `/api/conversations/:id/budget`.

## Cost Tracking (bonus)
- Per-turn: tokens in/out per LLM call, retrieval latency.
- Per-conversation: cumulative cost estimate (tokens × price table).
- Exposed in trace viewer.

## Controls to Prevent Runaway Cost
- Rewrite cap (default 2).
- Max LLM calls per turn (default 6).
- Retrieval timeout (default 5s).
- Rate limit (default 5 RPS per IP).
- Max answer tokens (default 1024).
- Max message length (default 4000 chars).
- Ingest batch size cap.

## Degradation
- On budget exceeded mid-generation: stop generation, return partial with `truncated: true`.
- On rate limit: 429 with Retry-After.
- On LLM error after retries: 502 with request id; no partial billing beyond what was consumed.

## Price Table (config, update periodically)
Stored in `config/config.yaml` under `pricing:` for cost estimation. Used only for reporting, not enforcement.
