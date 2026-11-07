# Models — Embeddings & LLMs — Blackbox

## Embedding Models
| Model | Dims | Use | Notes |
|---|---|---|---|
| `text-embedding-3-small` (OpenAI) | 1536 | default prod | good quality/cost |
| `text-embedding-3-large` (OpenAI) | 3072 | high-accuracy option | 2x cost |
| `all-MiniLM-L6-v2` (sentence-transformers) | 384 | dev/offline | free, lower quality |
| `bge-small-en-v1.5` | 384 | alt dev | free |

Selection is config-driven (`EMBEDDING_MODEL`). Chroma collection dimension must match; re-create collection on model change.

## LLM Models (by task)
| Task | Default (OpenAI) | Default (Anthropic) | Temp | Max tokens |
|---|---|---|---|---|
| Generation | gpt-4o | claude-sonnet-4-20250514 | 0.2 | 1024 |
| Grading | gpt-4o-mini | claude-3-5-haiku-20241022 | 0.0 | 256 |
| Rewrite | gpt-4o-mini | claude-3-5-haiku-20241022 | 0.3 | 256 |
| Summary | gpt-4o-mini | claude-3-5-haiku-20241022 | 0.2 | 512 |
| Entity extraction | gpt-4o-mini | claude-3-5-haiku-20241022 | 0.0 | 512 |
| Ground check | gpt-4o-mini | claude-3-5-haiku-20241022 | 0.0 | 512 |
| Direct answer (no retrieval) | gpt-4o-mini | claude-3-5-haiku-20241022 | 0.2 | 512 |

## Reranker
- `cross-encoder/ms-marco-MiniLM-L-6-v2` (local, sentence-transformers) — free after download, ~80MB.
- Alternative: LLM-based reranker (uses grading model) — slower/costlier; use only if cross-encoder unavailable.

## Provider Consistency Rule
Pick OpenAI OR Anthropic and stay consistent across all tasks. Do not mix providers within a deployment (avoids two key sets and inconsistent behavior). Configured via `LLM_PROVIDER=openai|anthropic`.

## Structured Output
- Grading/ground-check/entity extraction use structured output (JSON mode / tool calling) with Pydantic schemas. Parse with validation; retry once on failure.

## Token Counting
- Use `tiktoken` for OpenAI models; Anthropic token counter for Claude. Store per-model counter in config.

## Determinism
- Set `temperature=0` for grading/ground-check/entity extraction (deterministic).
- Generation allows small temperature (0.2) for natural phrasing.
- Seed not guaranteed across providers; accept minor variance.

## Fallback
- If primary provider errors (5xx/timeout), no automatic fallback to other provider in v1 (would require both keys). Document as limitation. Future: add fallback chain.
