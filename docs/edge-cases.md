# Edge Cases — Blackbox

## Retrieval
- **Empty corpus / no matches**: honest-failure path; state what was searched. Never hallucinate precedent.
- **Exact-token queries** (`ERR_POOL_EXHAUSTED`, `p99`): must use BM25; dense alone fails (assignment-critical).
- **Semantic queries** ("thing where requests pile up"): must use dense; BM25 alone fails.
- **Metadata filter with no results**: return empty, not error.
- **Vector store hang**: retrieval timeout (config, e.g. 5s); return partial/empty + flag.
- **Reranker failure**: fall back to fused order; log.
- **Duplicate chunks on re-ingest**: idempotent upsert by deterministic ID.
- **Very long query**: truncate to embedding model max tokens; log.
- **Unicode / special chars in error codes**: normalize consistently at ingest and query.

## Chunking
- Root Cause section split mid-sentence → forbidden; chunker must keep section atomic; if section exceeds max chunk size, split by sub-heading or sentence boundary, never mid-sentence.
- Missing sections in a doc: chunker skips gracefully, metadata marks absent sections.
- Empty document: skip with warning.
- Re-ingestion of modified doc: old chunks removed, new inserted (by doc_id).

## Memory
- **Ruled-out hypothesis re-suggested**: summary prompt must preserve ruled-out; verified in eval/demo.
- **Conflict**: "actually deploy was cause" after ruled out → update status to confirmed, remove contradiction. Demonstrated.
- **Token budget exceeded**: eviction order triggers; conversation stays coherent.
- **Very long conversation (40+ turns)**: summary compression + entity memory keep context; working memory bounded.
- **Entity memory empty on first turn**: injection skipped, no error.
- **Concurrent updates to same conversation**: serialize per conversation_id (Redis lock or DB transaction).

## Graph
- **Unbounded rewrite loop**: cap enforced; on cap → honest_failure, not infinite loop.
- **Grading LLM returns malformed**: parse defensively; default to "not sufficient" (safer) → rewrite or honest failure.
- **Ground check fails for all claims**: strip unsupported or return honest failure; never ship ungrounded claims.
- **Router misclassifies**: default to retrieval (safer than direct answer for incident questions).
- **LLM timeout / rate limit**: retry with backoff; surface error to user with request id.
- **Streaming client disconnect**: cancel graph execution; release resources.

## API
- **Missing/invalid conversation_id**: 404 / 400 with clear error.
- **Oversized message**: 413 with limit.
- **SSE connection drop**: client reconnect with Last-Event-ID (optional).
- **Ingest duplicate request**: idempotent; return counts.
- **Eval run while another running**: serialize or queue; return 409.

## Security
- **API key in logs**: scrubbed by logging filter.
- **Prompt injection in corpus or user query**: guardrails (see guardrails.md); retrieved chunks treated as data, not instructions.
- **PII in corpus**: not expected; if present, redact at ingest (optional).
- **CORS**: restrict to known origins in prod.

## Cost
- **Runaway rewrite loops**: cap + per-request token budget.
- **Dev iteration cost**: LLM caching; use cheap models for grading/rewrite.
- **Large corpus re-embed**: idempotent + skip unchanged (hash).

## Data
- **Postgres down**: degrade gracefully? Or fail fast with 503. Choose fail-fast with clear error.
- **Redis down**: fall back to in-memory cache / skip caching; log warning.
- **Chroma corruption**: rebuild from seed script.

## Large Documents / Streaming Ingestion
- **Oversized upload (>max_upload_size)**: reject 413 with clear message.
- **Memory exhaustion during ingest**: batched processing keeps peak bounded; if exceeded, abort job, clean temp files, return error.
- **Truncated/corrupt upload**: checksum/size validation; reject.
- **No headings (poor structure)**: fall back to fixed-size chunking with overlap; warn in trace.
- **Single huge section**: split by paragraph/sentence; never hold whole section in memory beyond batch.
- **Disk full during spool**: fail job, cleanup temp files.
- **Concurrent ingest jobs**: limit via semaphore; queue or 429.
- **Partial failure mid-job**: checkpoint per doc; resume from last successful doc.
- **Duplicate upload**: idempotent; return cached job result.

## Concurrency & State
- **Two users same conversation**: not supported (single engineer per conversation); document.
- **Clock skew for timestamps**: use server time.

## UI
- **No JS / old browser**: basic HTML fallback not required; document browser support.
- **Very long trace**: paginate or virtualize (optional).
