# Redis Cache Usage — Blackbox

## Role
Optional but recommended. Blackbox degrades gracefully if Redis is unavailable (falls back to in-memory / no cache) with a logged warning. Never a hard dependency for correctness.

## What Redis Caches
1. **LLM response cache** (dev primarily): keyed by `llm:{model}:{hash(prompt+params)}`. TTL configurable (default 24h in dev, disabled in prod or short TTL).
2. **Embedding cache**: keyed by `emb:{model}:{hash(text)}`. TTL long (7d) or none; invalidate on model change (key includes model).
3. **Retrieval result cache**: keyed by `ret:{hash(query+filters+topk)}`. TTL short (60s). Invalidate on ingest (flush `ret:*` or versioned keys).
4. **Rerank cache**: keyed by `rerank:{hash(query+chunk_ids)}`. TTL 5m.
5. **Conversation hot state**: working memory + entity memory keyed by `conv:{id}`. TTL 1h sliding; DB is source of truth, Redis is read-through cache.
6. **Rate limiting counters**: sliding window per IP/conversation.
7. **Distributed lock**: per-conversation lock for concurrent turn serialization.

## Key Design
- All keys namespaced with prefix `bb:` (e.g. `bb:ret:...`).
- Use Redis hashes for structured objects; JSON serialization otherwise.
- TTLs configured in `config/config.yaml`.

## Invalidation
- On ingest: bump a `corpus_version` counter; cache keys include version so stale entries naturally expire unused. Or explicit `DEL` of `ret:*`.
- On model change: version includes model name; old entries unused.
- On conversation update: update cache or invalidate `conv:{id}`.

## Connection & Resilience
- `redis-py` with connection pool.
- Timeout: 200ms for cache reads; on timeout, skip cache (treat as miss) and log.
- On connection failure at startup: log warning, run in no-cache mode. Health endpoint reports Redis status.
- Reconnect with backoff.

## Configuration
```yaml
redis:
  url: ${REDIS_URL}
  enabled: true
  timeouts_ms: 200
  ttl:
    llm_response_s: 86400
    embedding_s: 604800
    retrieval_s: 60
    rerank_s: 300
    conversation_state_s: 3600
```

## When NOT to Use Redis
- Single-process dev with small corpus: in-memory caches suffice; Redis optional.
- If Redis unavailable in prod: degrade to no cache; latency/cost increase but correctness preserved.

## Security
- Redis AUTH enabled in prod; URL includes password.
- No secrets stored in cache values.
- Cache keys must not contain raw user PII if PII present; hash inputs.
