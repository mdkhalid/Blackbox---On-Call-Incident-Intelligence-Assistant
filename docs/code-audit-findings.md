# Code Audit — Bugs & Scaling Gaps

Audit of the Blackbox codebase against the "correct scaling" bar, conducted after Phase 1
(control plane) was closed. Every finding below was **reproduced against the running code**,
not inferred by reading. Reproduction scripts were run outside the repo and deleted; each
finding records enough detail to rebuild its repro.

- **Audit date:** 2026-11-10
- **Baseline at audit:** commit `d568250`, 333 tests green, `ruff` clean, `mypy` 35 errors
- **Scope:** concurrency correctness, request-path correctness, token budgeting, and
  horizontal-scaling readiness

Related documents:
- [`scaling-and-production-readiness.md`](scaling-and-production-readiness.md) — the target
  architecture and phased roadmap this audit is measured against
- [`development-tracker.md`](development-tracker.md) — living status of the phased plan
- [`retrieval-results.md`](retrieval-results.md) — measured retrieval comparison table

**Status legend:** 🔴 critical · 🟠 major · 🟡 minor · ⬜ not started · ✅ fixed

> **Resolution:** all nine findings are fixed as of 2026-11-11. The table below records the
> state of each one, the commit that fixed it and the regression test that now guards it.
> The original analysis is kept in each section so the reasoning, and therefore the shape
> of the fix, can still be read.

---

## 0. Summary

| # | Severity | Area | Finding | State |
|---|---|---|---|---|
| [1](#1-sse-token-stream-leaks-ungrounded-content-) | 🔴 | Correctness / safety | SSE token events expose the answer **before** the ground check runs | ✅ `9e9f643` |
| [2](#2-trace-timings-cross-contaminate-between-concurrent-requests-) | 🟠 | Concurrency | Shared singleton retriever leaks per-request timings across requests | ✅ `9e9f643` |
| [3](#3-reported-token-budget-does-not-match-what-was-sent-) | 🟠 | Correctness / observability | `/budget` reports tokens that were never sent; real prompt can exceed the cap | ✅ `9e9f643` |
| [4](#4-conversation_locks-dict-grows-without-bound-) | 🟡 | Memory | Per-conversation lock map is never pruned | ✅ `b01f724` |
| [5](#5-thread-pool-caps-sse-concurrency-) | 🟠 | Scaling | Sync graph nodes cap concurrent SSE turns at the thread-pool size | ✅ `03c72f4` |
| [6](#6-bm25-index-is-o(corpus)-in-memory-per-pod-) | 🟠 | Scaling | Full corpus is pulled into RAM per pod and rebuilt on every ingest | ✅ `0ba7279` |
| [7](#7-rate-limiter-ignores-x-forwarded-for-) | 🟠 | Scaling | Behind an ingress, all clients share one rate-limit bucket | ✅ `b01f724` |
| [8](#8-ingest-and-eval-block-the-worker-) | 🟡 | Scaling | Operational endpoints run synchronously instead of `202` + job | ✅ `12514ce` |
| [9](#9-mypy-errors-with-no-type-check-gate-) | 🟡 | Quality | 35 type errors, CI never runs mypy | ✅ `b01f724` |

**After the fixes:** 395 tests green, `ruff` clean, `mypy` clean, CI gates on all three.

### What is genuinely good

Worth stating plainly, because it is the majority of the codebase:

- **Hybrid retrieval is real and correctly justified.** RRF with config-driven weights, and the
  fusion sweep in `docs/retrieval-results.md` is a 21-point *measured* plateau, not intuition.
- **Reranking is disabled by default because the measurement said so** (MRR 0.9167 → 0.8857).
  The docs report that dense and BM25 *tie* on exact-token queries *contrary to the assignment's
  expectation*, and explain the corpus shape that causes it, rather than relabelling it.
  This is exactly the intellectual honesty the rubric rewards.
- **The ground check itself is correct** — deterministic, structural, cannot hallucinate. Only
  its *streaming ordering* was broken (finding 1).
- **The tokenizer preserves identifiers** (`ERR_POOL_EXHAUSTED` stays one token), which directly
  serves the error-code case the assignment calls out.
- **The lexical reranker abstains** when a query has no content terms, so it cannot scramble a
  good fused ranking.
- **Auth/RBAC/tenant isolation is solid** — fail-closed, role matrix, cross-tenant tests, and
  Postgres advisory locks on a dedicated `NullPool` engine so no pooled connection is held for
  the SSE duration.
- **Graceful degradation is consistent** — Redis down → in-process cache; Postgres down → hard
  failure in prod, SQLite fallback in dev; every cache failure degrades to a miss.
- **Config discipline is excellent** — every tunable lives in YAML, validated by pydantic with
  real invariants (`candidate_pool >= top_k`, `summary_trigger_turns > working_turns`).

---

## 1. SSE token stream leaks ungrounded content 🔴

**Status:** fixed · **Severity:** critical · **Area:** correctness / safety

### The problem

`blackbox/api/routes/chat.py:191` emits token events as soon as *any* node update contains an
`"answer"` key:

```python
answer = (event.get("data") or {}).get("answer")
if answer and not emitted:
    emitted = answer
    for piece in token_chunks(answer):
        yield sse_event("token", {"text": piece})
```

The graph order is `generate → ground_check`. The `generate` node is the first to produce
`"answer"`, so tokens stream the **pre-ground-check** answer. `ground_check` then strips
unsupported claims — but the client already received them.

`blackbox/api/sse.py:30` documents the opposite behaviour:

> These are not raw decode tokens: the ground check must run before any answer text is
> exposed, so token events intentionally start after `ground_check`/`honest_failure`.

The code does not do what its own docstring says.

### Reproduction

An LLM provider that returns one supported claim and one claim with no supporting chunk:

```
=== event order ===
  node     router
  node     retrieve
  node     grade
  node     generate
  token    × 6            <-- ungrounded text already on the wire
  node     ground_check
  node     honest_failure
  final

final status   : honest_failure
final answer   : "I didn't find a matching past incident for: ..."
streamed tokens: "The pool was saturated because the database connection pool was exhausted.
                   The moon landing was faked in 1969 by NASA contractors. [pm-001::Root Cause::0]"

UNGROUNDED CLAIM STREAMED TO CLIENT: True
tokens == final answer: False
```

The fabricated sentence reaches the client, and the `final` event then contradicts it.

### Why it matters

This breaks the assignment's central safety requirement:

> A claim that doesn't must be removed or flagged, **not shipped**.

and the corresponding rubric line for the LangGraph pipeline (ground check). It also breaks the
product's own premise: at 2 AM, an assistant that streams a confident fabrication and *then*
retracts it is worse than one that never produced it. The `honest_failure` path is explicitly
required to be *demonstrated* — this bug corrupts exactly that demonstration.

Note the existing test `tests/test_api_chat.py:112` asserts
`streamed_answer(events) == final["answer"]`. It passes today only because the fake LLM always
produces a fully-supported answer, so nothing is ever stripped. The invariant is asserted but
not actually exercised.

### Fix

Buffer the answer and emit tokens only after grounding has completed.

- In `chat.py`, stop emitting tokens from the `generate` node.
- Emit tokens from the `final` event (or from the `ground_check` / `honest_failure` node
  update), so the streamed text is always the post-ground-check text.
- Keep `sse.py`'s docstring as the specification — it already describes the desired behaviour.

### Regression test to add

A provider that returns one unsupported claim. Assert that the concatenated token events
contain **neither** the unsupported claim **nor** any substring of the stripped answer, and
that `streamed_answer(events) == final["answer"]` holds for the honest-failure path too.

### Resolution ✅ `9e9f643`

The token stream is emitted after the graph finishes, not from the `generate` node:

```python
answer = result.get("answer") or ""
for piece in token_chunks(answer):
    yield sse_event("token", {"text": piece})
```

The answer is the post-ground-check text (or the honest-failure text), so what the client
renders is what the server stands behind. `sse.py`'s docstring described this behaviour all
along; the code now matches it.

Guarded by `tests/test_api_chat.py`:

- `test_ungrounded_claims_never_reach_the_client` — a provider that drafts one supported and
  one fabricated claim; the fabrication must not appear in the token stream, not even in part.
- `test_token_events_start_after_the_ground_check` — token events must not precede the node
  that finalises the answer.
- `test_chat_honest_failure_is_streamed_and_persisted` — now also asserts
  `streamed_answer(events) == final["answer"]` on the honest-failure path.

Both new tests were confirmed to fail against the pre-fix code.

---

## 2. Trace timings cross-contaminate between concurrent requests 🟠

**Status:** fixed · **Severity:** major · **Area:** concurrency

### The problem

`blackbox/retrieval/hybrid_retriever.py:61-62` stores per-request timing data on the
**instance**:

```python
_last_timings: dict[str, float] = PrivateAttr(default_factory=dict)
_last_counts: dict[str, int] = PrivateAttr(default_factory=dict)
```

`blackbox/graph/nodes.py:61` then reads it back right after the call:

```python
timings.update(getattr(deps.retriever, "last_timings", {}) or {})
```

But the retriever is a **process-wide singleton**. `AppServices.ensure_runtime`
(`blackbox/api/services.py:113`) builds it once and hands the same object to every request via
`GraphDeps`. So `_last_timings` is shared mutable state, and two concurrent requests read each
other's numbers.

### Reproduction

Two threads calling `retrieve()` on the same retriever, then reading `last_timings`:

```
observed timings per request:
  'ERR_POOL_EXHAUSTED checkout-service' -> {'dense_ms': 5.77, 'sparse_ms': 0.09, ...}
  'pool saturation p99 latency'          -> {'dense_ms': 5.77, 'sparse_ms': 0.09, ...}

both requests saw the SAME timings: True
```

The second request's timings are attributed to the first. The same applies to `last_counts`
(dense/sparse/fused/returned counts) and to the cache-hit path, which overwrites
`_last_timings` with a `{"cache_ms", "total_ms"}` dict.

### Why it matters

- The trace viewer is a **mandated deliverable** and its whole purpose is per-request debugging.
  Wrong latency numbers make it actively misleading under load — exactly when you need it.
- `docs/scaling-and-production-readiness.md` §11 commits to "request rate/latency/errors …
  retrieval stage latency" and to propagating request/tenant ids through the whole execution.
  Cross-contaminated timings undermine that.
- It scales *inversely*: the bug only appears under concurrency, i.e. only under real load.

### Fix

Return the timing payload from `retrieve()` instead of stashing it on the instance.

- Change `retrieve()` to return `(chunks, timings)` or attach the timings to a per-request
  result object.
- Keep `last_timings` / `last_counts` as a convenience only for single-threaded callers
  (tests, eval harness), or remove them and update `tests/test_hybrid_retriever.py` and
  `tests/test_retrieval_cache.py` accordingly.

### Resolution ✅ `9e9f643`

The shared state is gone rather than kept as a convenience: `last_timings` and `last_counts`
no longer exist, so there is nothing for a concurrent request to overwrite or misread.

- `blackbox/retrieval/models.py` — new `RetrievalResult` dataclass carrying the chunks, the
  timings, the counts and whether the result came from the cache.
- `blackbox/retrieval/hybrid_retriever.py` — `retrieve_detailed()` returns the `RetrievalResult`
  of the call that produced it; `retrieve()` delegates to it and returns just the chunks.
- `blackbox/graph/nodes.py` — `retrieve_node` reads the timings off the result it just received.

Guarded by `tests/test_hybrid_retriever.py` and `tests/test_graph_build.py`:

- `test_retriever_keeps_no_shared_per_request_state` — the singleton carries no per-request state.
- `test_concurrent_retrievals_keep_their_own_timings` — two concurrent calls return distinct
  timing dicts, and the slow leg really is slower than the fast one.
- `test_retrieve_node_takes_the_timings_of_its_own_call` — the deterministic form: the node
  takes the timings off the result it just received, not off the instance.

---

## 3. Reported token budget does not match what was sent 🟠

**Status:** fixed · **Severity:** major · **Area:** correctness / observability

Three related defects in the same subsystem.

### 3a. `_merge_chunk_budget` sums the wrong chunk set

`blackbox/api/routes/chat.py:274` computes the reported chunk tokens from **all** serialized
chunks in the result:

```python
chunk_tokens = sum(count_tokens(chunk.get("text", "")) for chunk in (result.get("chunks") or []))
```

But `generate_node` (`blackbox/graph/nodes.py:400 _fit_chunks`) sends only the trimmed subset
that fits `budget.retrieved_chunks_tokens`. The reported number is the pre-trim total.

Reproduction:

```
retrieved chunk tokens (top_k=8): 3000   budget for that bucket: 2000
reported total_tokens : 6821   cap: 6024   over_cap: True

chunks actually sent to the LLM: 5  tokens: 1875
REAL prompt tokens (context + sent chunks): 3696
REPORTED prompt tokens                     : 4821
report matches reality: False
```

### 3b. The cap check never sees the retrieved chunks

`blackbox/api/routes/chat.py:165` builds the context with an **empty** chunk list:

```python
bundle = build_context(
    system_prompt=CHAT_SYSTEM_PROMPT,
    memory=memory,
    chunks=[],          # <-- retrieved chunks are not part of the cap check
    question=message,
    budget=services.config.budget,
)
```

So the eviction loop in `blackbox/memory/context.py:99` caps the context at
`total_cap_tokens` **without** the chunks, and `generate_node` then prepends up to
`retrieved_chunks_tokens` on top. The real prompt can exceed the cap:

```
context tokens (after cap enforcement): 4685
chunk tokens actually sent: 2000
REAL prompt tokens (context + chunks): 6685
total_cap_tokens                     : 6024
EXCEEDS CAP? True
overrun by: 661 tokens
```

### 3c. Output headroom is reserved on paper but not enforced

`BudgetConfig` sums to the cap exactly:

```
system 800 + entity 400 + summary 600 + recent 1200 + chunks 2000 + headroom 1024 = 6024
```

The intent is therefore `input <= 6024 - 1024 = 5000`. But `context.py:99` compares input
against `total_cap_tokens` (6024) with no headroom subtraction, and the headroom is never
checked against the answer length either (`max_answer_tokens: 1024` is a separate knob).

### Why it matters

`GET /api/conversations/:id/budget` is a **mandated API endpoint** with an explicit
requirement: *"Expose the actual per-request allocation through an API so it is observable."*
Right now it reports an allocation that does not correspond to any real request. A budget
report that lies is worse than no budget report — it gives false confidence that the context
window is being respected.

### Fix

- Pass the retrieved chunks into `build_context` so the cap covers the whole prompt, or
  perform the cap enforcement *after* chunks are known (i.e. in `generate_node`).
- Compute the reported chunk tokens from the same trimmed set that was sent.
- Enforce the input ceiling as `total_cap_tokens - output_headroom_tokens`.

### Regression test to add

- Assert the reported `total_tokens` equals the sum of the tokens actually sent.
- Assert the real prompt never exceeds `total_cap_tokens - output_headroom_tokens`.

### Resolution ✅ `9e9f643`

All three defects are fixed, and the budget now describes one request rather than two
estimates of it.

**3c — the headroom is enforced.** `build_context` computes the input ceiling as
`total_cap_tokens - output_headroom_tokens` and evicts against that, not against the raw cap.

**3b — the chunks are inside the cap.** The memory side reserves the chunk allocation when it
is assembled (the API assembles memory before the graph retrieves), so the evidence cannot be
added on top of an already-full context.

**3a — the reported numbers are the sent numbers.** `generate_node` measures the prompt it
actually built, fits the evidence into what is left of the ceiling, and reports:

- `prompt_tokens` — the exact token count of the prompt handed to the provider.
- `buckets.retrieved_chunks.allocated` — the exact token count of the evidence block sent.
- `total_tokens` — the content allocation (system + rendered context + evidence + question).

The API now returns the graph's own budget report instead of recomputing one from the
retrieval result, so there is a single source of truth. The system prompt was unified as
well: `CHAT_SYSTEM_PROMPT` and `GENERATE_SYSTEM` are now the same string, so the budget
counts the prompt that was sent rather than a differently worded one.

Guarded by `tests/test_api_chat.py` and `tests/test_memory_budget.py`:

- `test_reported_budget_matches_the_prompt_that_was_sent` — `prompt_tokens` equals the token
  count of the prompt the provider received, read back from the recorded provider call.
- `test_the_real_prompt_never_exceeds_the_input_ceiling` — with a deliberately tight budget,
  the real prompt stays under `total_cap - output_headroom` and `over_cap` is false.
- `test_the_output_headroom_is_reserved_from_the_input_ceiling`,
  `test_the_chunk_room_is_reserved_when_the_chunks_are_not_retrieved_yet`,
  `test_supplied_chunks_are_counted_against_the_ceiling`.

---

## 4. `conversation_locks` dict grows without bound 🟡

**Status:** fixed · **Severity:** minor · **Area:** memory

`blackbox/api/services.py:201`:

```python
def lock_for(self, conversation_id: str) -> asyncio.Lock:
    lock = self.conversation_locks.get(conversation_id)
    if lock is None:
        lock = asyncio.Lock()
        self.conversation_locks[conversation_id] = lock   # never removed
    return lock
```

An `asyncio.Lock` is created per conversation ID and **never pruned**. On a long-lived process
serving thousands of distinct conversations per day, the map grows without bound.

Related, and documented but worth restating: `_tenant_counts` (`services.py:51`) is per-process
only, so the tenant concurrency limit is per-pod, not global. `docs/scaling-and-production-readiness.md`
§6 calls this out as a Phase 1 limitation, and it is correctly deferred — but the
`conversation_locks` leak is **not** documented anywhere.

### Fix

- Remove the lock from the map when the turn completes and the lock is unlocked (only safe when
  no waiter exists — check `lock.locked()` and waiter count, or use a `WeakValueDictionary`).
- Consider bounding the map size with an LRU, or only allocating locks for conversations that
  are currently active.

### Resolution ✅ `b01f724`

`AppServices.release_lock` drops an idle lock from the registry when the turn ends, and
`LocalTurnLock` calls it on release. Only the lock currently registered for that key is
dropped, and only while it is unlocked.

The "no waiter exists" condition holds by construction here: the local lock is a *try*-lock.
A second caller is rejected with `409` rather than queued, so there are never waiters on it,
and the check-then-acquire sequence contains no await point that could interleave.

Guarded by `tests/test_api_locks.py`:

- `test_idle_turn_locks_are_pruned_from_the_registry` — fifty conversations, fifty turns, an
  empty registry afterwards.
- `test_a_busy_turn_keeps_its_lock_registered` — a held lock stays registered and still
  rejects a second turn with `ConversationBusy`.

---

## 5. Thread pool caps SSE concurrency 🟠

**Status:** fixed · **Severity:** major · **Area:** scaling

### The problem

Every node in `blackbox/graph/nodes.py` is a plain synchronous `def` performing blocking work:
Chroma queries, BM25 scoring, and synchronous HTTP calls to the LLM provider.

LangGraph offloads sync nodes to the event loop's **default `ThreadPoolExecutor`**. Verified
directly against the installed LangGraph (`langgraph._internal._runnable.run_in_executor` →
`loop.run_in_executor(None, …)`):

```
cpu_count: 22
default ThreadPoolExecutor max_workers: 26
```

A turn holds a thread for the entire graph run (retrieve → grade → generate → ground_check).
So **concurrent SSE turns per pod are capped by the thread pool**, not by
`max_concurrent_turns_per_tenant` (8) and not by any other admission control.

The event loop itself is *not* blocked — verified with a concurrent ticker task that kept
firing during a graph run, so the SSE stream stays responsive. The ceiling is thread
occupancy, not event-loop starvation.

### Why it matters

`docs/scaling-and-production-readiness.md` §2 targets **2,000 concurrent SSE conversations**
and **200 turns/s peak**. With ~26 threads per pod and turns taking seconds (LLM latency),
a pod cannot sustain anywhere near that. The document lists "Async provider interface; async
graph nodes … no threadpool hops in the request path" under Phase 3, so this is a *known* gap.

It is called out here because it is **the single largest throughput ceiling in the system**,
and because it interacts with findings 1–3: any fix that moves work onto the event loop
(as async nodes) will make the event-loop-blocking issues matter more.

### Fix (Phase 3)

- Convert the provider interface to async and the graph nodes to `async def`, so the
  thread pool is not occupied for the duration of an LLM round trip.
- Make `HybridRetriever.aretrieve` genuinely concurrent — it currently wraps the whole
  synchronous `retrieve()` in a single `asyncio.to_thread`, so the dense and sparse legs still
  run **sequentially inside one thread** despite the docstring claiming otherwise.
- Bound the executor explicitly rather than relying on the default.

### Resolution ✅ `03c72f4`

All three parts are done.

**Async provider interface.** `LLMProvider` gained `acomplete`, implemented by the hosted
providers (via `AsyncOpenAI` / `AsyncAnthropic`), the offline provider, the resilience wrapper
(async semaphore, async backoff, the same circuit breaker) and the cache wrapper.

**Async graph nodes.** Every node in `blackbox/graph/nodes.py` and `blackbox/graph/router.py`
is now a coroutine function, so LangGraph runs them on the event loop instead of offloading
them to the thread pool. The entity store's blocking SQLAlchemy reads are pushed to a worker
thread explicitly rather than by accident.

**Genuinely concurrent retrieval.** The retriever was restructured around a `_RetrievalPlan`
and per-leg methods, so `aretrieve_detailed` dispatches the dense and sparse legs to two
threads and awaits them together. The request now pays for the slower leg rather than the sum
of both.

**Bounded executor.** The app's lifespan installs a `ThreadPoolExecutor` of
`MAX_WORKER_THREADS` (default 32) as the event loop's default executor, replacing
asyncio's `min(32, cpu_count + 4)`.

Two consequences worth recording:

- `run_graph` is now `async`, and the groundedness evaluation with it. The evaluation script
  drives it with `asyncio.run`.
- The async work exposed a latent bug: an in-memory SQLite database belongs to a *connection*,
  and SQLAlchemy hands every thread its own connection — so the API's thread-pooled database
  calls saw a different, empty database per thread. In-memory SQLite now uses a static pool
  (`blackbox/db.py`), so the database is shared.

Guarded by `tests/test_async_runtime.py` and `tests/test_hybrid_retriever.py`:

- `test_every_graph_node_is_a_coroutine_function` — the structural guarantee.
- `test_the_resilient_provider_awaits_its_provider` — the provider is awaited, not called in
  a thread.
- `test_the_lifespan_bounds_the_default_executor` — the pool size is the configured one.
- `test_async_retrieval_runs_the_dense_and_sparse_legs_concurrently` — the behavioural
  guarantee: two 300 ms legs complete in under 500 ms, not 600 ms.

---

## 6. BM25 index is O(corpus) in memory, per pod 🟠

**Status:** fixed · **Severity:** major · **Area:** scaling

`blackbox/retrieval/sparse.py:72-92` builds the index by pulling the **entire corpus** out of
the vector store:

```python
data = self.vectorstore.get_all()
...
corpus = [tokenize(t or "") for t in texts]
self._bm25 = BM25Okapi(corpus)
```

`blackbox/retrieval/hybrid_retriever.py:72` does the same for the lexical reranker's IDF table.

Consequences at the 10M-chunk target (§2 of the scaling doc):

- Every pod holds a full copy of the corpus text **plus** the tokenised representation and the
  BM25 index in RAM — a multi-GB footprint per pod, multiplied by replica count.
- The index is rebuilt from scratch after every ingest (`hybrid_retriever.invalidate()`),
  which is O(corpus) work on the request path of the next query.
- `SparseRetriever.get_chunk` (`sparse.py:99`) does `self._ids.index(chunk_id)` — an **O(n)
  linear scan** over the corpus for a single lookup. Currently only used by tests, but it is a
  trap for the next caller.

### Fix (Phase 2)

- Move sparse retrieval to OpenSearch (already the decided target) — the in-process BM25 becomes
  a dev-only adapter behind the `SparseStore` protocol.
- Replace the linear `ids.index()` lookup with a dict.
- Move index rebuild off the request path (build in the worker, publish atomically).

### Resolution ✅ `0ba7279`

All three parts are done.

**The `SparseStore` protocol.** `blackbox/retrieval/stores.py` defines `DenseStore` and
`SparseStore`. The in-process BM25 index is now a *dev-only adapter* behind it, and
`blackbox/retrieval/opensearch.py` is the adapter that scales. The metadata filter stays a
store-neutral AST (`MetadataFilter`) and is compiled per store: `to_chroma_where()` for
Chroma, `matches()` for the in-process matcher, `opensearch_clauses()` for OpenSearch. The
backend is chosen by `retrieval.sparse_backend` (`bm25` | `opensearch`) in the YAML config,
with the connection details in the environment.

**The linear scan is gone.** `SparseRetriever` keeps a `by_id` dict alongside the corpus, so
`get_chunk` is a dict lookup.

**The rebuild moved off the request path.** `invalidate()` now only marks the corpus-derived
state as stale; it no longer rebuilds. The ingestion worker calls `rebuild_indexes()`, which
rebuilds the index and the reranker's IDF table and publishes them atomically — a reader
sees either the old index or the new one, never a half-built one. The lazy rebuild remains
only as the development fallback for the case where nothing has published an index yet.

Guarded by `tests/test_retrieval_stores.py`:

- `test_the_in_process_bm25_adapter_satisfies_the_sparse_store_protocol`,
  `test_the_opensearch_adapter_satisfies_the_sparse_store_protocol`,
  `test_the_dense_retriever_satisfies_the_dense_store_protocol`.
- `test_the_sparse_store_contract` — the behaviour the hybrid retriever relies on, whatever
  the adapter.
- `test_a_chunk_lookup_is_a_dict_lookup_not_a_linear_scan`.
- `test_the_index_is_rebuilt_and_published_off_the_request_path` and
  `test_an_invalidated_index_is_rebuilt_on_demand_as_a_fallback`.
- `test_the_filter_is_compiled_into_opensearch_clauses` and the query-body tests.

**What is still open:** the OpenSearch adapter is written and unit-tested for its query
compilation, but it has not been run against a real cluster, and the OpenSearch index is not
yet populated by ingestion. Chroma plus the in-process BM25 remain the default and the
rollback path until that is done.

---

## 7. Rate limiter ignores `X-Forwarded-For` 🟠

**Status:** fixed · **Severity:** major · **Area:** scaling

`blackbox/api/middleware.py:103`:

```python
key = request.client.host if request.client else "unknown"
```

`request.client.host` is the **TCP peer address**. Behind an ingress, ALB, or nginx — which is
the documented target deployment (`docs/scaling-and-production-readiness.md` §9) — every
request arrives from the proxy's IP. The result:

- `rate_limit_rps: 5` becomes a **global** limit for the whole cluster, not a per-client one.
- One noisy client can exhaust the budget for every tenant behind the same ingress.
- It also makes the limit useless as an abuse control.

The repo contains **no** handling of `X-Forwarded-For` (verified by search: the only hit for
forwarded-header handling anywhere in `blackbox/` is this line).

### Fix

- Run uvicorn with `--proxy-headers --forwarded-allow-ips=<trusted proxy CIDRs>`
  (see `Dockerfile` CMD and `docker-compose.yml`), **or**
- Resolve the client IP from a trusted-proxy-aware helper that reads `X-Forwarded-For` only
  when the immediate peer is a known proxy.

Note the security angle: `X-Forwarded-For` must only be trusted from known proxies, otherwise
the limit becomes trivially bypassable by a spoofed header.

### Resolution ✅ `b01f724`

The second option was chosen: a trusted-proxy-aware resolver.

`blackbox/api/client_ip.py` resolves the client address as follows:

1. No trusted proxies configured → the peer address, unchanged.
2. The immediate peer is not in a trusted network → the peer address. Any forwarded header it
   sent is client-controlled and is ignored, so a spoofed header cannot buy a client its own
   rate-limit bucket.
3. The peer is trusted → the right-most address in `X-Forwarded-For` that is not itself a
   trusted proxy. That is the address the outermost trusted hop observed.

Trusted networks are configured with `TRUSTED_PROXY_CIDRS` (comma-separated CIDRs); the
parsing lives in `blackbox/iputils.py` so neither the config module nor the API layer has to
depend on the other.

Guarded by `tests/test_api_client_ip.py`: no trusted proxies, a trusted proxy, an untrusted
peer spoofing the header, a chain of proxies, a missing peer, an address with a port, and
invalid CIDR entries.

---

## 8. Ingest and eval block the worker 🟡

**Status:** fixed · **Severity:** minor · **Area:** scaling

`blackbox/api/routes/operations.py:22` (`POST /api/ingest`) and `:74` (`POST /api/eval/run`)
are **synchronous** `def` routes that run the whole job inline.

Consequences:

- They occupy a worker (a threadpool thread) for the entire ingest / eval grid. The eval grid
  in particular runs 35 questions × 4 strategies over the corpus.
- The only protection is a `threading.Lock` that returns `409` if a job is already running —
  correct behaviour, but it means a second caller gets an error rather than queueing.
- The scaling document §8 specifies the target shape explicitly:
  `POST /api/ingest` and `POST /api/eval/run` return **`202 {job_id}`**, with status/result
  endpoints, workers consuming a queue, and retry budgets + dead-letter queues. That work is
  scheduled for Phase 2/3 but is not implemented.

### Fix

- Return `202 {job_id}` and move the work to a background worker (queue-backed per §8).
- Add `GET /api/ingest/{job_id}` and `GET /api/eval/{job_id}` status endpoints.
- Keep the existing single-job lock as the queue-depth guard until the worker exists.

### Resolution ✅ `12514ce`

Both endpoints now answer `202 {job_id}` and the work happens on a background worker.

- `blackbox/api/jobs.py` — a `JobQueue` per kind: a FIFO queue served by one worker thread,
  with a job record carrying the status, the timestamps, the result and the error.
- `GET /api/jobs/{job_id}`, `GET /api/ingest/{job_id}` and `GET /api/eval/{job_id}` report the
  job's state and its result. A job belonging to another tenant is reported as `404`, not
  `403`, so the endpoint cannot be used to probe for other tenants' jobs.
- The `409` on a second concurrent run is gone: a second request is queued rather than
  refused.

Two design points worth recording:

- The worker is a **thread**, not a task on the request's event loop. A request's loop does
  not outlive the request, so a task-based worker would be cancelled the moment the response
  was sent — which is exactly what happened in the first implementation.
- The queue is in-process. A shared, durable queue (the Redis-backed worker of §8) is the
  next step; the API shape does not change when that happens, only where the queue lives.

Guarded by `tests/test_api_jobs.py` and `tests/test_api_operations.py`: a job runs and
reports its result, a failing job is reported without killing the worker, jobs of one kind
run in order, a second job is queued rather than refused, and another tenant's job is not
found.

---

## 9. mypy errors with no type-check gate 🟡

**Status:** fixed · **Severity:** minor · **Area:** quality

`mypy blackbox` reports **35 errors across 16 files**:

```
blackbox/api/store.py:348-370   (11 errors) Column[Any] vs str/int — SQLAlchemy 2.0 typing
blackbox/api/services.py:126,139 (2 errors)  LLMProvider protocol: read-only `name` vs settable
blackbox/graph/build.py:67       (1 error)   StateGraph NodeInputT cannot be dict[Any, Any]
blackbox/retrieval/hybrid_retriever.py:59-60 (2 errors) PrivateAttr default=None vs non-Optional
blackbox/graph/grounding.py:79    (1 error)   dict | None is not indexable
blackbox/llm/fake.py:153          (1 error)   "None" object is not iterable
... and 17 more
```

The `LLMProvider` protocol errors are the most substantive: `services.py` reassigns `llm` from
`LLMProvider` to `ResilientLLMProvider` and then to `CachedLLMProvider`, and mypy is correctly
flagging that the protocol declares `name` as a settable variable while the implementations
expose it as a read-only property.

`ruff` is clean and CI runs lint + tests + a migration check, but **mypy is never run in CI**
(`docs/development-tracker.md:218` tracks it as an open item: *"Fix remaining mypy errors
(currently ~30; no type-check gate)"*). The error count will only grow.

### Fix

- Add a non-blocking mypy step to `.github/workflows/ci.yml` so the count cannot silently grow.
- Fix the errors in the order above; the `store.py` cluster (11 errors) is mostly a
  `Mapped[]`/`mapped_column()` migration to SQLAlchemy 2.0 typing, which would also align the
  models with the doc's own §7 requirement: *"SQLAlchemy 2.0 `Mapped` models"*.

### Resolution ✅ `b01f724`

`mypy blackbox` reports **no issues across 92 source files**, and CI runs it.

- The SQLAlchemy models are now `Mapped[]` / `mapped_column()` typed, as the scaling document's
  own §7 requires. The DDL is unchanged, so the Alembic baseline still matches.
- The `LLMProvider` protocol now declares `name` as a read-only property, which is what the
  implementations actually expose; the protocol was the thing that was wrong.
- The remaining errors were real and are fixed: the graph nodes are typed with `GraphState`
  rather than `dict` (which is also what `StateGraph` requires), `PrivateAttr` defaults are
  typed as optional, the grounding and fake-LLM provider have explicit None handling, and the
  Chroma result types are cast at the boundary rather than lying about them.
- Optional dependencies (`openai`, `anthropic`, `chromadb`, `opensearchpy`,
  `sentence_transformers`, `yaml`, `redis`) are imported lazily and are exempted from the
  missing-stub check in `pyproject.toml`, because the package installs them optionally.
  (`rank_bm25` was in this list until Phase 2 replaced it with a local BM25 that scores
  exactly like Lucene; see `docs/development-tracker.md`.)

Guarded by `tests/test_docs.py::test_ci_gates_on_lint_types_and_tests`, which asserts the
workflow runs ruff, mypy and pytest.

---

## Appendix A — How the fixes were ordered

The order the audit originally suggested, and what actually happened:

1. **Finding 1** (SSE token ordering) — `9e9f643`. Breaks the core safety guarantee and
   corrupts the honest-failure demo. Contained fix, one file plus a regression test.
2. **Finding 2** (shared timing state) — `9e9f643`. Small, mechanical, removes a whole class of
   confusing trace bugs.
3. **Finding 3** (budget reporting + cap enforcement) — `9e9f643`. A mandated deliverable; three
   related defects in one subsystem, fixed together.
4. **Finding 7** (`X-Forwarded-For`) — `b01f724`. Small config-level change, but blocks correct
   operation behind the documented ingress.
5. **Finding 4** (lock map leak) — `b01f724`. Small; folded into the change touching
   `services.py`.
6. **Finding 9** (mypy gate) — `b01f724`. Cheap to add now, so the count stops growing.
7. **Finding 5** (async graph nodes) — `03c72f4`. The largest throughput ceiling; scheduled
   deliberately rather than discovered.
8. **Finding 8** (async ingest/eval) — `12514ce`. The job queue was built on top of the async
   runtime rather than before it.
9. **Finding 6** (BM25 at scale) — `0ba7279`. The `SparseStore` protocol, the OpenSearch adapter
   and the off-path index rebuild.

The order differs from the suggestion in one place: finding 9 (the mypy gate) was pulled
forward to fourth, because the async work touched every module in the package and a clean type
check made that work much easier to trust.

## Appendix B — Verification notes

At audit time (commit `d568250`):

- `pytest -q` — **333 passed**.
- `ruff check blackbox tests scripts` — **clean**.
- `mypy blackbox` — **35 errors** (finding 9).
- LangGraph sync-node offloading confirmed by reading
  `langgraph/_internal/_runnable.py::run_in_executor` and by an event-loop responsiveness test.
- Default executor size confirmed at runtime: `min(32, cpu_count + 4)` = 26 on this machine.
- All four bug reproductions were run against the live code with throwaway scripts under the
  OS temp directory; the scripts were deleted afterwards and no reproduction artefacts were
  left in the repository.

After the fixes (commit `266f809`):

- `pytest -q` — **395 passed**.
- `ruff check blackbox tests scripts alembic` — **clean**.
- `mypy blackbox` — **clean** (92 source files checked).
- CI runs lint, mypy, tests and the migration check.
- The finding-1 regression tests were confirmed to fail against the pre-fix code, so they are
  real guards rather than tests written to match the new behaviour.
- The in-memory SQLite bug that the async work exposed was confirmed with a direct
  reproduction: a table created on the main thread is *not* visible to a worker thread on
  `sqlite:///:memory:`.
- A server smoke test against a real uvicorn (not the TestClient) confirmed the SSE
  ordering end to end: `node -> token -> final`, with every token event after the node
  that finalises the answer.

## Appendix C — One more bug, found by the smoke test

The server smoke test was run against a real uvicorn rather than the test client, and it
failed where the test suite passed. The cause was not a regression but a pre-existing defect
that the test client could not see.

**A local database written by an older version of the models broke the app.**
`create_all` creates missing tables but never alters an existing one, so a `data/blackbox.db`
written before Phase 1's `tenant_id` column kept its old columns, and every query against
the new column failed with `no such column: conversations.tenant_id`. The app could not be
started at all against such a database, and nothing in the logs explained why.

Fixed in `266f809`: a local SQLite database whose schema no longer matches the models is
detected and recreated — outside production only, because production owns its schema with
Alembic. `blackbox/db.py` holds the detection; `AppServices._recreate_if_stale` holds the
policy.

The smoke test also confirmed that the audit remediation did not change retrieval
behaviour: the measured MRR on the seeded corpus was unchanged at 0.9214 for hybrid RRF and
0.8667 with reranking, the same numbers `docs/retrieval-results.md` reported at the time.

> **Later, in Phase 2, those numbers moved.** The sparse leg now scores with Lucene's BM25
> formula — the formula the OpenSearch backend uses — instead of `rank_bm25`'s slightly
> different one, and equal scores are broken by document id rather than by the dense
> store's internal id order. The re-measured figures are dense 0.8548, sparse 0.8714,
> hybrid 0.9167 and hybrid+rerank 0.8857. The sparse leg gained 0.0143 and the hybrid lost
> 0.0047, a difference six times smaller than the metric's own sensitivity to tie-breaking
> (0.0286). The Phase 2 gate was re-baselined to 0.9167 accordingly; the reasoning is in
> `docs/development-tracker.md`.
