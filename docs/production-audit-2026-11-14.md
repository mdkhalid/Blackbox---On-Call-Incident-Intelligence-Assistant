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

Re-verified 2026-11-23 at commit `b87d5ec`: 608 collected (596 passing, 12 skipped),
`ruff`/`mypy` clean, coverage 83.5%. The five criticals and the fixed highs below remain
fixed, and all seven code-doable items in §3/§4 were closed on 2026-11-23 (each section
records its resolution). Only infrastructure-bound validation (Terraform apply, the
live-cluster benchmark, load/chaos/DR runs) and the two residual maintainability notes in §4
remain.

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

## 3. Verified highs — fixed 2026-11-23

- ✅ **Ground check verifies only declared claims.** `verify_answer` now walks every answer
  sentence and traces it to a cited or best-matching chunk; `strip_unsupported_sentences`
  rebuilds the answer from supported sentences only. Long prose outside `claims` no longer
  ships unverified.
- ✅ **Readiness is a single check.** `/health/ready` reports `storage`, `runtime`,
  `retrieval`, `llm` and `cache` separately, pings storage with `SELECT 1`, retries warm-up
  while cold, and exports `blackbox_runtime_ready` / `blackbox_dependency_up`.
- ✅ **Lock/slot/idempotency release is not cancellation-safe.** Cleanup is a shielded,
  per-release-isolated `_release_turn`; a cancellation test proves the lock, slot and key
  are still released.
- ✅ **Job worker uses `asyncio.run` per job.** Each worker thread owns one long-lived event
  loop; `_invoke` reuses it, so loop-bound SDK/Redis clients stay valid across jobs.
- ✅ **Evaluation precision@k divides by `len(top)`.** The denominator is now `k`; the
  comparison table and README were regenerated (P@5 dense 0.3029, sparse 0.2629, hybrid
  0.2914, hybrid+rerank 0.3143).
- ✅ **Shadow retrieval is awaited, not background.** Both sync and async paths dispatch the
  comparison and return immediately; a latency-isolation test covers both.

## 4. Design-pattern deviations (maintainability)

- ◐ `JobStore.dequeue` now takes the kind and the `isinstance` branch is gone; the
  `DenseStore` filter compilation still lives in `DenseRetriever`.
- ◐ Job bodies still live in the API route module; `worker.py` imports it for registration.
- ⬜ `AppServices` stores every seam as `Any`, so protocols are not checked at the
  composition root. A `Retriever` protocol now exists and is asserted for the hybrid and
  shadow retrievers, but the container is not typed against it yet.
- ✅ Five separate "Redis or fall back" factories with divergent enable flags. They now use
  one `blackbox/redis_factory.py` for client construction.
- ✅ Dead settings removed: `RERANKER_MODEL`, `ChunkingConfig`, `entity_max_tokens`,
  unused `IngestionConfig` caps, `RegionConfig.failover_generation`.

## 5. What is genuinely production-grade

Auth/RBAC and tenant-scoped reads; SSE ordering for the final answer; deterministic
ground check and honest-failure text; resilient LLM wrapper; usage/cost accounting;
bounded metric labels; Alembic chain with upgrade/downgrade tests; retrieval eval parity
gate; provider contract tests; opt-in store integration tests; the CI gates.

---

## 6. Recommended order for the remaining work

1. ✅ Ground-check sentence verification (correctness of the safety claim).
2. ✅ Readiness with per-dependency state and warm-up retry.
3. ✅ Cancellation-safe release of lock/slot/idempotency.
4. ✅ Job worker event loop (lease/DLQ semantics remain a separate feature).
5. ✅ Precision@k correction + regenerated `docs/retrieval-results.md`.
6. ◐ Design-pattern consolidation: retriever protocol, `JobStore.dequeue(kind)` and the
   Redis factory are done; typing `AppServices` against the protocols and moving job bodies
   out of the route module remain.

The residual items are infrastructure-bound (apply Terraform, live backend benchmark,
load/chaos/DR runs) and the optional bonus features; see `development-tracker.md` §12.
