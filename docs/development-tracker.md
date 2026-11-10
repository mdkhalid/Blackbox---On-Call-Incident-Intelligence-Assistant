# Blackbox Development Tracker

Living document for all work beyond the original assignment. Update this file with every
meaningful change so the repository always shows what is done, what is in flight, and what is
next.

- **Architecture and production contract:** [`scaling-and-production-readiness.md`](scaling-and-production-readiness.md)
- **Completed assignment phases:** [`phase-wise-plan.md`](phase-wise-plan.md)
- **Status legend:** ✅ done · ◐ in progress · ⬜ not started · ⚠ blocked

Last updated: 2026-11-10

---

## 1. Baseline

| Item | State |
|---|---|
| Original assignment phases 0–9 | ✅ complete |
| Tests | 333 passing, `ruff` clean |
| Runtime | FastAPI + SSE, LangGraph corrective RAG, three-layer memory, trace viewer |
| Auth | JWT/RBAC + tenant-scoped conversations implemented; dev-disabled by default |
| Retrieval | local Chroma + in-process BM25 (single node) |
| Stores | Postgres/SQLite conversation + entity store |
| Deployment | Docker Compose (two-replica smoke passing) |
| Production target | Kubernetes, org-wide, multi-region, strict tenant isolation |

## 2. Current status (2026-11-10)

**Phase 1 — complete (control plane).**
- Auth/tenancy: JWT/RBAC, tenant-scoped conversations and turns, cross-tenant tests.
- Persistence: Alembic baseline with upgrade/downgrade test; ad-hoc DDL removed; migration,
  lint and test jobs added to GitHub Actions.
- Shared state: Redis rate limits, in-flight `Idempotency-Key` dedupe, result replay, and
  per-process per-tenant turn admission control.
- Resilience: LLM retry with jitter, circuit breaker, bounded concurrency; request-body cap;
  real readiness check; audit log lines for chat/ingest/eval.
- Exit gate met: 333 tests green, `ruff` clean, and a two-replica Docker smoke passed (pod 1
  writes a conversation, pod 2 reads it from shared Postgres; the replicas share one Redis
  rate-limit bucket).
- Scope decision: data-plane tenant isolation (retrieval predicates and Postgres RLS) moves
  to Phase 2, where the retrieval stores are replaced. Phase 1 isolates the control plane.

**Next:** Phase 2 — retrieval migration and mandatory tenant filters.
**Not started:** Phase 3 async/workers/traces, Phase 4 Kubernetes/observability, Phase 5
multi-region/DR.

### Resume point (next session)

State: clean working tree at `9efb2c4`; **333 tests green**, `ruff` clean; Docker available;
Phase 1 closed. Nothing is uncommitted.

**First Phase 2 package (do this next):**
1. Add `DenseStore` / `SparseStore` protocols plus a neutral metadata-filter AST in
   `blackbox/retrieval/stores.py`. Wrap the current Chroma and BM25 implementations as
   adapters without changing behavior, with contract tests against the existing corpus.
2. Put `tenant_id` into chunk metadata at ingestion and require it in retrieval filters
   (reject an empty tenant). Backfill/re-ingest the seed corpus and add cross-tenant retrieval
   tests: tenant A must never retrieve tenant B chunks, even with an empty/forged filter.
3. Add the Qdrant dense adapter and contract tests (Docker/testcontainers), then the OpenSearch
   sparse adapter. Keep Chroma/BM25 as the default and rollback path until eval parity is green.
4. Only then start shadow indexing and dual-read cutover.

Do not begin alias cutover before the filtered-retrieval contract and tenant-isolation proof
are green. Phase 2 exit gate: MRR ≥ 0.9214 (or documented baseline), tenant filters proven,
p95 within SLO.

**Commands:**
```bash
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check blackbox tests scripts alembic
.\.venv\Scripts\python.exe scripts\multi_pod_smoke.py   # Docker; shared Postgres + Redis
```

**Open decision before adapter work:** Qdrant+OpenSearch vs OpenSearch-only (benchmark with
the eval harness). Tenant corpus strategy: one shared collection with mandatory tenant
payloads vs per-tenant indexes — decide before the Qdrant adapter lands.

## 3. Status dashboard

| Workstream | Phase | State | Notes |
|---|---|---|---|
| Production/scaling document | 0 | ✅ | `docs/scaling-and-production-readiness.md` |
| HTTP body limits | 1 | ✅ | `BodyLimitMiddleware`, configurable bytes |
| LLM timeout/retries | 1 | ✅ | passed to OpenAI/Anthropic clients |
| Dedicated lock pool | 1 | ✅ | `NullPool` engine for Postgres advisory locks |
| Readiness checks storage | 1 | ✅ | `/health/ready` returns 503 when storage fails |
| Auth (JWT + RBAC) | 1 | ✅ | dev-disabled, prod-required; HS256 dev / JWKS RS256 |
| Tenant-scoped conversations | 1 | ✅ | app-layer isolation; RLS/retrieval predicates are Phase 2 |
| Alembic migration baseline | 1 | ✅ | all tables + upgrade/downgrade test; CI check added |
| Shared Redis rate limits | 1 | ✅ | fixed-window shared counter, local fallback |
| Chat idempotency keys | 1 | ✅ | result replay + in-flight dedupe via Redis `SET NX` |
| LLM resilience wrapper | 1 | ✅ | jittered retries, circuit breaker, bounded concurrency |
| Per-tenant admission control | 1 | ✅ | per-process counters; global quotas Phase 2 |
| Multi-pod smoke test | 1 | ✅ | two replicas, shared Postgres read + shared Redis limit |
| Qdrant/OpenSearch migration | 2 | ⬜ | adapters, tenant filters, dual-read, alias cutover |
| Async graph + workers | 3 | ⬜ | remove threadpool from request path |
| Trace store split | 3 | ⬜ | Postgres recent + ClickHouse/S3 |
| Kubernetes/Helm/Terraform | 4 | ⬜ | EKS reference, HPA/KEDA |
| Observability stack | 4 | ⬜ | OTel, Prometheus/Grafana/Loki |
| CI/CD + security scanning | 4 | ⬜ | GitHub Actions + GitOps |
| Multi-region + DR | 5 | ⬜ | tenant-pinned writes, failover drills |
| Load/chaos suites | 5 | ⬜ | target SLO gates |

---

## 3. Phase 1 — Foundations (complete, control plane)

Exit gate met: authorization matrix green, shared rate/idempotency state, LLM resilience,
Alembic migration check in CI, two-replica smoke passing, full suite green. Data-plane tenant
isolation is a Phase 2 gate because the retrieval stores are replaced there.

### 3.1 Documentation and guardrails
- [x] Production/scaling document with target architecture and SLOs
- [x] Request-body cap before JSON parsing (`guardrails.max_request_bytes`)
- [x] LLM timeout/retry settings passed to hosted clients
- [x] Readiness checks the conversation store
- [x] Dedicated `NullPool` engine for Postgres advisory locks

### 3.2 Authentication and tenancy
- [x] Add OIDC/JWT verification dependency (`blackbox/api/auth.py`)
- [x] Roles `viewer` / `operator` / `admin`; admin-only ingest/eval
- [x] Add `tenant_id` to conversations and turns (Alembic baseline)
- [x] Tenant-scoped repository methods; cross-tenant reads return 404
- [x] Authorization matrix tests (cross-tenant read/write/ingest → 403/404)
- [x] Audit events for chat, ingest, eval and admin actions
- [ ] Mandatory tenant predicate in retrieval filters; reject empty tenant — **Phase 2**
- [ ] Postgres RLS policies as defense in depth — **Phase 2**
- [ ] Delete/rotate tenant credentials and offboarding — **Phase 2**

### 3.3 Persistence
- [x] Add Alembic (dependency, `alembic.ini`, baseline revision, upgrade/downgrade test)
- [x] Remove ad-hoc `ALTER TABLE` DDL; local/test bootstrap uses `create_all` only
- [x] Indexes on `tenant_id` and `conversation_id` in the baseline migration
- [x] Migration check job in CI (`upgrade head` + `downgrade base`)
- [ ] SQLAlchemy 2.0 `Mapped` models; JSONB + partition plan for turns/traces — **Phase 2**
- [ ] Retention/deletion API and tenant-offboarding job — **Phase 2**

### 3.4 Shared state and concurrency
- [x] Redis fixed-window rate limiter with in-process fallback
- [x] `Idempotency-Key` support on `/api/chat` (final response replay)
- [x] In-flight idempotency dedupe with a Redis `SET NX` lock (409 while in flight)
- [x] Shared keys tenant-scoped for chat idempotency
- [x] Max concurrent turns per tenant/pod; 429 with `Retry-After` under backpressure
- [x] Multi-pod Docker smoke: two replicas, one conversation, shared Redis limit

### 3.5 LLM resilience
- [x] Retry with jitter, circuit breaker and bounded concurrency (`ResilientLLMProvider`)
- [x] Fake-provider tests for retry recovery and circuit-open failure
- [ ] Async `acomplete` provider interface — **Phase 3**
- [ ] Async graph/providers with no threadpool hops — **Phase 3**
- [ ] Explicit token/cost quotas per tenant — **Phase 4**

---

## 4. Phase 2 — Retrieval migration and data-plane isolation

Exit gate: eval parity (MRR ≥ 0.9214 or documented baseline), tenant filters proven, p95
within SLO at target load.

- [ ] `DenseStore` / `SparseStore` protocols
- [ ] Qdrant adapter (async, payload filters, batch upsert, snapshots)
- [ ] OpenSearch adapter (async, BM25, filter compilation, index aliases)
- [ ] Neutral metadata-filter AST; compile per store
- [ ] Mandatory tenant clause injected after compilation; reject empty tenant
- [ ] Postgres RLS policies as defense in depth
- [ ] Concurrent dense/sparse legs (`asyncio.gather`)
- [ ] Keep score trail, fusion, rerank and eval harness unchanged
- [ ] Versioned indexes + `chunks-current` alias; staged ingest and atomic cutover
- [ ] Shadow indexing and dual-read comparison
- [ ] Eval parity gate wired into CI
- [ ] Remove Chroma/BM25 runtime dependency after cutover (rollback window documented)
- [ ] Benchmark Qdrant+OpenSearch vs OpenSearch-only; record decision
- [ ] SQLAlchemy 2.0 `Mapped` models; JSONB + partition plan; retention/deletion APIs
- [ ] Tenant offboarding removes conversations, turns, entities, traces and index payloads
- [ ] Global per-tenant LLM/token quotas (per-pod admission control already in place)

## 5. Phase 3 — Async runtime and jobs

- [ ] Async graph nodes and LangGraph execution
- [ ] Async retrieval stores + provider clients end-to-end
- [ ] Queue worker: ingest, eval, summary compaction
- [ ] `202 + job_id` ingest/eval APIs with status endpoints
- [ ] Staged ingest with validation and alias swap
- [ ] Trace store split: Postgres recent + ClickHouse/S3 archive
- [ ] Paginated `/trace`; retention jobs
- [ ] Graceful shutdown/drain and SSE cancellation tests

## 6. Phase 4 — Kubernetes and operations

- [ ] Helm chart: API, workers, HPA/KEDA, PDB, NetworkPolicies
- [ ] Terraform: EKS, Aurora, ElastiCache, SQS/MSK, OpenSearch, Qdrant, S3, KMS, WAF
- [ ] Ingress SSE config: buffering off, idle timeout, body cap, TLS
- [ ] Startup/readiness/liveness probes and warm-up
- [ ] OpenTelemetry traces/metrics/logs; Prometheus/Grafana/Loki/Tempo
- [ ] Dashboards, alerts, SLO burn
- [x] GitHub Actions: lint, tests, migration check
- [ ] CI: mypy, coverage, image scan/SBOM, image build
- [ ] GitOps + canary/blue-green; migration Job
- [ ] Load tests at target concurrency; failure injection
- [ ] Pen-test checklist and secret rotation

## 7. Phase 5 — Multi-region and DR

- [ ] Tenant home-region routing (DNS/GSLB)
- [ ] Regional Postgres with cross-region DR; failover runbook
- [ ] Qdrant/OpenSearch per-region replication and rebuild path
- [ ] Redis regional, non-authoritative
- [ ] Tenant move procedure
- [ ] RTO/RPO drills with measured results
- [ ] Cost model per tenant/conversation; budgets and quotas
- [ ] Chaos suite: pod/AZ/store/provider failures

---

## 8. Cross-cutting backlog

- [ ] Fix remaining mypy errors (currently ~30; no type-check gate)
- [ ] Coverage threshold and coverage report in CI
- [ ] Provider contract tests (OpenAI/Anthropic against mock servers)
- [ ] Postgres/Redis integration tests (testcontainers)
- [ ] Docker image build in CI (local two-replica smoke passes)
- [ ] Load test script (k6/Locust) committed
- [ ] PII redaction and query-log policy
- [ ] Security headers and dependency scanning
- [ ] Update README quick start for production mode (auth + external stores)

---

## 9. Decision log

| Date | Decision | Status | Notes |
|---|---|---|---|
| 2026-11-10 | Phase 1 closes the control plane; retrieval tenant predicates + RLS are Phase 2 | Accepted | Stores are replaced in Phase 2, so the filter is proven there |
| 2026-11-10 | Per-pod tenant admission control; global quotas later | Provisional | Redis global counters are Phase 2 |
| 2026-11-10 | Kubernetes, AWS EKS reference, org-wide target | Accepted | Portable via Helm |
| 2026-11-10 | Keep application-side RRF fusion | Accepted | Preserves measured 0.9214 MRR |
| 2026-11-10 | Qdrant (dense) + OpenSearch (sparse) | Provisional | Revisit after Phase 2 benchmark |
| 2026-11-10 | Postgres advisory locks on dedicated NullPool engine | Accepted | Avoids SSE holding pooled connections |
| 2026-11-10 | Tenant-pinned conversation writes across regions | Accepted | Avoids cross-region write conflicts |
| 2026-11-10 | Traces split to ClickHouse/S3 | Provisional | Final choice before Phase 3 |
| 2026-11-10 | Provisional SLOs (2k SSE, 200 turns/s, 10M+ chunks) | Provisional | Replace with measured capacity |

## 10. Open questions

- Cloud provider and data-residency regions (AWS assumed).
- RPO/RTO targets and tenant tiers.
- Embedding dimension/model and quantization policy.
- Qdrant + OpenSearch vs OpenSearch-only.
- ClickHouse vs S3+Athena for trace analytics.
- Tenant migration procedure between regions.

---

## 11. Change log

Append one row per merged PR or meaningful local milestone. Do not delete history.

| Date | Change | State | Commit/PR |
|---|---|---|---|
| 2026-11-10 | Original assignment phases 0–9 complete; 300 tests | ✅ | `7ca5250` |
| 2026-11-10 | Production/scaling document created | ✅ | local |
| 2026-11-10 | Tracker created | ✅ | local |
| 2026-11-10 | Body limit, LLM timeout/retries, NullPool lock engine | ✅ | local |
| 2026-11-10 | JWT auth/RBAC, tenant-scoped conversations, cross-tenant tests | ✅ | local |
| 2026-11-10 | Alembic baseline migration + upgrade/downgrade test | ✅ | local |
| 2026-11-10 | Redis rate limiter + chat idempotency replay | ✅ | local |
| 2026-11-10 | In-flight idempotency, per-tenant admission, LLM resilience wrapper | ✅ | local |
| 2026-11-10 | CI workflow (lint, tests, migration check) + audit event lines | ✅ | local |
| 2026-11-10 | Two-replica Docker smoke (shared Postgres + Redis) passing | ✅ | local |
| 2026-11-10 | Added Phase 2 resume point: store protocols, tenant metadata, adapters | ✅ | `9efb2c4` |

## 12. Update protocol

1. Move items between `⬜`, `◐`, `✅` and add a one-line note with the commit/PR.
2. Add a row to the change log; never rewrite past rows.
3. Add new work under the correct phase; add a new phase only for a genuinely new workstream.
4. Record architecture decisions in the decision log and mirror major ones in
   `docs/scaling-and-production-readiness.md`.
5. Keep the status dashboard consistent with the checklists; CI/test counts must be current.
