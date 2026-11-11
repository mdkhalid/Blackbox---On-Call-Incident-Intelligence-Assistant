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

**Status legend:** 🔴 critical · 🟠 major · 🟡 minor · ⬜ not started

---

## 0. Summary

| # | Severity | Area | Finding |
|---|---|---|---|
| [1](#1-sse-token-stream-leaks-ungrounded-content-) | 🔴 | Correctness / safety | SSE token events expose the answer **before** the ground check runs |
| [2](#2-trace-timings-cross-contaminate-between-concurrent-requests-) | 🟠 | Concurrency | Shared singleton retriever leaks per-request timings across requests |
| [3](#3-reported-token-budget-does-not-match-what-was-sent-) | 🟠 | Correctness / observability | `/budget` reports tokens that were never sent; real prompt can exceed the cap |
| [4](#4-conversation_locks-dict-grows-without-bound-) | 🟡 | Memory | Per-conversation lock map is never pruned |
| [5](#5-thread-pool-caps-sse-concurrency-) | 🟠 | Scaling | Sync graph nodes cap concurrent SSE turns at the thread-pool size |
| [6](#6-bm25-index-is-o(corpus)-in-memory-per-pod-) | 🟠 | Scaling | Full corpus is pulled into RAM per pod and rebuilt on every ingest |
| [7](#7-rate-limiter-ignores-x-forwarded-for-) | 🟠 | Scaling | Behind an ingress, all clients share one rate-limit bucket |
| [8](#8-ingest-and-eval-block-the-worker-) | 🟡 | Scaling | Operational endpoints run synchronously instead of `202` + job |
| [9](#9-mypy-errors-with-no-type-check-gate-) | 🟡 | Quality | 35 type errors, CI never runs mypy |

### What is genuinely good

Worth stating plainly, because it is the majority of the codebase:

- **Hybrid retrieval is real and correctly justified.** RRF with config-driven weights, and the
  fusion sweep in `docs/retrieval-results.md` is a 21-point *measured* plateau, not intuition.
- **Reranking is disabled by default because the measurement said so** (MRR 0.9214 → 0.8667).
  The docs report that dense beats BM25 on exact-token queries *contrary to the assignment's
  expectation*, and explain the corpus shape that causes it, rather than relabelling it.
  This is exactly the intellectual honesty the rubric rewards.
- **The ground check itself is correct** — deterministic, structural, cannot hallucinate. Only
  its *streaming ordering* is broken (finding 1).
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

**Status:** open · **Severity:** critical · **Area:** correctness / safety

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

---

## 2. Trace timings cross-contaminate between concurrent requests 🟠

**Status:** open · **Severity:** major · **Area:** concurrency

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

---

## 3. Reported token budget does not match what was sent 🟠

**Status:** open · **Severity:** major · **Area:** correctness / observability

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

---

## 4. `conversation_locks` dict grows without bound 🟡

**Status:** open · **Severity:** minor · **Area:** memory

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

---

## 5. Thread pool caps SSE concurrency 🟠

**Status:** open (partially covered by Phase 3) · **Severity:** major · **Area:** scaling

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

---

## 6. BM25 index is O(corpus) in memory, per pod 🟠

**Status:** open (Phase 2) · **Severity:** major · **Area:** scaling

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

---

## 7. Rate limiter ignores `X-Forwarded-For` 🟠

**Status:** open · **Severity:** major · **Area:** scaling

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

---

## 8. Ingest and eval block the worker 🟡

**Status:** open (Phase 2/3) · **Severity:** minor · **Area:** scaling

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

---

## 9. mypy errors with no type-check gate 🟡

**Status:** open (tracked) · **Severity:** minor · **Area:** quality

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

---

## Appendix A — Suggested fix order

1. **Finding 1** (SSE token ordering) — breaks the core safety guarantee and corrupts the
   honest-failure demo. Contained fix, one file plus a regression test.
2. **Finding 2** (shared timing state) — small, mechanical, removes a whole class of confusing
   trace bugs.
3. **Finding 3** (budget reporting + cap enforcement) — a mandated deliverable; three related
   defects in one subsystem, best fixed together.
4. **Finding 7** (`X-Forwarded-For`) — small config-level change, but blocks correct operation
   behind the documented ingress.
5. **Finding 8** (async ingest/eval) — needed for Phase 2/3; already specified in the doc.
6. **Finding 5** (async graph nodes) — Phase 3, but it is the largest throughput ceiling and
   should be scheduled deliberately rather than discovered.
7. **Finding 6** (BM25 at scale) — Phase 2, mitigated by the OpenSearch migration.
8. **Finding 4** (lock map leak) — small; fold into any change touching `services.py`.
9. **Finding 9** (mypy gate) — cheap to add now, so the count stops growing.

## Appendix B — Verification notes

- `pytest -q` — **333 passed** at audit time.
- `ruff check blackbox tests scripts` — **clean**.
- `mypy blackbox` — **35 errors** (finding 9).
- LangGraph sync-node offloading confirmed by reading
  `langgraph/_internal/_runnable.py::run_in_executor` and by an event-loop responsiveness test.
- Default executor size confirmed at runtime: `min(32, cpu_count + 4)` = 26 on this machine.
- All four bug reproductions were run against the live code with throwaway scripts under the
  OS temp directory; the scripts were deleted afterwards and no reproduction artefacts were
  left in the repository.
