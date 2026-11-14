# Production Readiness Audit — 2026-11-14

Second full audit, run after Phase 5. Six independent passes covered architecture and
design patterns, production readiness, the data layer and concurrency, retrieval and
ingestion, the API/graph/memory path, and code hygiene.

Unlike the 2026-11-10 audit, findings here were **not all reproduced by execution**.
Every item marked **verified** below was re-read in the source by the lead reviewer
after the delegated passes; items marked **reported** come from a single pass and are
tracked for confirmation.

Baseline at audit: commit `0b4c982`, 587 tests collected, `ruff`/`mypy` clean,
coverage 82.7%.

**Headline:** the architecture is sound and the design patterns are largely good, but
the service was **not production-ready**: five verified criticals, including a safety
regression (ungrounded claims on the wire) and an inert tenant-isolation backstop that
the documentation had claimed was live. Those claims are corrected here.

**Status legend:** ✅ fixed · ◐ in progress · ⬜ open · ⚠ reported, unverified

---

## 1. Verified criticals

### C1. Row-level security was never armed ✅ fixed
`set_current_tenant()` existed (`blackbox/db.py`) with an `after_begin` hook, but no
middleware, route or worker called it — only tests did. The shipped regional overlays
set `RLS_ENABLED=true`, so with a non-owner role every row was invisible, and with the
table owner RLS was silently bypassed because `FORCE` was absent.

**Fix:** the conversation repository now binds the tenant around every session
(`_session(tenant_id)` / `_read_session(tenant_id)` → `set_current_tenant`), and
migration `0006_rls_force` adds `FORCE ROW LEVEL SECURITY` to the three tenant tables.
A regression test asserts the repository enters the tenant context.

### C2. Ungrounded claims reached the client before the ground check ✅ fixed
`public_update` removed only `trace` and `answer`, so the `generate` node's `claims`
(and `ground_check`'s `verdicts`, which contain unsupported claim text) were emitted as
node events before the check ran. The existing safety test only inspected `token`
events, so it stayed green.

**Fix:** `public_update` excludes every model-output-derived field (`answer`, `claims`,
`verdicts`, `trace`). The guard test now asserts over the entire event stream.

### C3. Cross-tenant destructive admin endpoints ✅ fixed
`DELETE /api/admin/tenants/{tenant_id}` and `POST /api/admin/retention` ignored the
principal's tenant, and `delete_older_than` was cluster-wide. Any tenant admin could
delete another tenant's data.

**Fix:** offboarding requires the path tenant to equal the principal's tenant (or the
`platform_admin` role), retention is scoped to the principal's tenant end-to-end, and
`delete_older_than`/`delete_turn_ids` take a tenant predicate. Cross-tenant tests added.

### C4. Ingestion writes were not tenant-scoped ✅ fixed
Chunk ids were `sha256(doc_id::section::part)` and `delete_by_doc`/`has_doc` filtered on
`doc_id` alone. Two tenants ingesting the same document name deleted or overwrote each
other, and the second was told "skipped".

**Fix:** chunk ids include the tenant for non-default tenants (the default tenant keeps
the historical id space so measured baselines stay valid), every presence/delete filter
is tenant-scoped in Chroma and Qdrant, and the ingest ledger/doc-entity keys are
tenant-prefixed.

### C5. Two application service containers per process ✅ fixed
`main.py` built one `AppServices` for middleware and the lifespan built a second,
swapping `app.state.services`. That meant two DB pools per pod, a middleware bound to
the orphan container, and the first container never closed.

**Fix:** exactly one container is built and stored; middleware resolves it from
`app.state` per request, and the duplicate settings assignment is gone.

---

## 2. Verified highs — fixed in this pass

- **Month-end quota crash** ✅ `_seconds_until_utc_midnight` executed `day + 1` before
  its dead branch; on the last day of a month, quota-enabled chat raised.
- **Retrieval deadline not enforced** ✅ the graph calls `aretrieve_detailed`, which had
  no deadline wrapper. The graph now enforces `retrieval_timeout_ms` with
  `asyncio.wait_for`.
- **Qdrant broke hybrid fusion** ✅ `get_all()` returned uuid point ids while queries
  returned `chunk_id`, so the two legs never merged. `get_all` now returns the payload
  chunk id; delete filters include the tenant.
- **Memory compression could wipe the summary** ✅ an empty/shorter LLM response
  overwrote the cumulative summary and then dropped the turns. It now keeps the old
  summary and drops nothing when the response is not usable.
- **PII control was inert** ✅ `PII_REDACTION_ENABLED` was never read and retrieval
  logged `query[:120]` at INFO. Query log sites now route through
  `summarize_query`/`redact_pii` according to policy.

## 3. Verified highs — still open

- ⬜ **Ground check verifies only declared claims.** Answer prose absent from `claims`
  ships unverified; `strip_unsupported` is a substring replace, so paraphrased clauses
  can survive. Needs sentence-level verification with citations.
- ⬜ **Readiness is a single check.** `ensure_storage` short-circuits after the first
  success; vector store, Redis, lock engine and executor saturation are not checked and
  a failed warm-up leaves the pod NotReady indefinitely.
- ⬜ **Lock/slot/idempotency release is not cancellation-safe.** Releases live in the
  response generator's `finally`; an exception before streaming or a cancelled task can
  leak the advisory lock and tenant slot.
- ⬜ **Job worker uses `asyncio.run` per job**, reusing loop-bound SDK/Redis clients
  across loops; sequential LLM-backed jobs can fail.
- ⬜ **Evaluation precision@k divides by `len(top)`** after document dedupe, inflating
  the shipped P@5 column. Needs the formula fix and a regenerated comparison table.
- ⬜ **Shadow retrieval is awaited, not background**, doubling latency when enabled.

## 4. Design-pattern deviations (maintainability)

- ◐ `DenseStore` seam is fictional: Chroma filter compilation lives in `DenseRetriever`,
  so Qdrant is not substitutable. `JobStore.dequeue` is unimplementable in the Redis
  adapter, and the caller branches on `isinstance`.
- ◐ Job bodies live in the API route module; `worker.py` imports a route module for its
  registration side effect.
- ⬜ `AppServices` stores every seam as `Any`, so protocols are not checked at the
  composition root.
- ⬜ Five separate "Redis or fall back" factories with divergent enable flags.
- ⬜ Dead settings: `RERANKER_MODEL`, most of `ChunkingConfig`, `entity_max_tokens`,
  `IngestionConfig` caps, `RegionConfig.failover_generation`.

## 5. What is genuinely production-grade

Auth/RBAC and tenant-scoped reads; SSE ordering for the final answer; deterministic
ground check and honest-failure text; resilient LLM wrapper; usage/cost accounting;
bounded metric labels; Alembic chain with upgrade/downgrade tests; retrieval eval parity
gate; provider contract tests; opt-in store integration tests; the CI gates.

---

## 6. Recommended order for the remaining work

1. Ground-check sentence verification (correctness of the safety claim).
2. Readiness with per-dependency state and warm-up retry.
3. Cancellation-safe release of lock/slot/idempotency.
4. Job worker event loop and lease/DLQ semantics.
5. Precision@k correction + regenerate `docs/retrieval-results.md`.
6. Design-pattern consolidation (retriever protocol, job registry, Redis factory).
