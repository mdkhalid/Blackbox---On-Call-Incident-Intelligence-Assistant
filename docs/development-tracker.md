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
| Tests | 324 passing, `ruff` clean |
| Runtime | FastAPI + SSE, LangGraph corrective RAG, three-layer memory, trace viewer |
| Auth | JWT/RBAC + tenant-scoped conversations implemented; dev-disabled by default |
| Retrieval | local Chroma + in-process BM25 (single node) |
| Stores | Postgres/SQLite conversation + entity store |
| Deployment | Docker Compose only |
| Production target | Kubernetes, org-wide, multi-region, strict tenant isolation |

## 2. Current status (2026-11-10)

**Done**
- Original assignment phases 0–9: complete.
- Production Phase 0 (architecture/scaling/production document): complete.
- Phase 1 foundations: request-body cap, LLM timeout/retry settings, dedicated NullPool
  lock engine, real readiness check.
- Phase 1 auth/tenancy: JWT/RBAC, tenant-scoped conversations and turns, cross-tenant tests.
- Phase 1 persistence: Alembic baseline migration (all app tables) with upgrade/downgrade test.
- Phase 1 shared state: Redis-backed rate limiter with local fallback; `/api/chat`
  `Idempotency-Key` result replay.

**Left in Phase 1**
- Postgres RLS; mandatory tenant filters in retrieval stores.
- Retire ad-hoc DDL for tenant columns; add CI migration check.
- In-flight idempotency dedupe (two simultaneous requests with the same key both execute).
- Async LLM circuit-breaker/semaphore wrapper; max concurrent turns per tenant.
- Multi-pod Compose/kind smoke test and audit events.

**Not started:** Phase 2 retrieval migration, Phase 3 async/workers/traces, Phase 4
Kubernetes/observability/CI, Phase 5 multi-region/DR.

## 3. Status dashboard

| Workstream | Phase | State | Notes |
|---|---|---|---|
| Production/scaling document | 0 | ✅ | `docs/scaling-and-production-readiness.md` |
| HTTP body limits | 1 | ✅ | `BodyLimitMiddleware`, configurable bytes |
| LLM timeout/retries | 1 | ✅ | passed to OpenAI/Anthropic clients |
| Dedicated lock pool | 1 | ✅ | `NullPool` engine for Postgres advisory locks |
| Readiness checks storage | 1 | ✅ | `/health/ready` returns 503 when storage fails |
| Auth (JWT + RBAC) | 1 | ✅ | dev-disabled, prod-required; HS256 dev / JWKS RS256 |
| Tenant-scoped conversations | 1 | ◐ | schema + repository isolation done; RLS/Alembic check pending |
| Alembic migration baseline | 1 | ✅ | all tables + upgrade/downgrade test |
| Shared Redis rate limits | 1 | ✅ | fixed-window shared counter, local fallback |
| Chat idempotency keys | 1 | ◐ | result replay done; in-flight dedupe pending |
| Async LLM wrapper | 1 | ⬜ | circuit breaker, semaphore, retry policy |
| Multi-pod smoke test | 1 | ⬜ | Compose/kind + authorization matrix |
| Qdrant/OpenSearch migration | 2 | ⬜ | adapters, dual-read, alias cutover |
| Async graph + workers | 3 | ⬜ | remove threadpool from request path |
| Trace store split | 3 | ⬜ | Postgres recent + ClickHouse/S3 |
| Kubernetes/Helm/Terraform | 4 | ⬜ | EKS reference, HPA/KEDA |
| Observability stack | 4 | ⬜ | OTel, Prometheus/Grafana/Loki |
| CI/CD + security scanning | 4 | ⬜ | GitHub Actions + GitOps |
| Multi-region + DR | 5 | ⬜ | tenant-pinned writes, failover drills |
| Load/chaos suites | 5 | ⬜ | target SLO gates |

---

## 3. Phase 1 — Foundations (in progress)

Exit gate: authorization matrix green, tenant filters fail closed, Alembic migration check in
CI, multi-pod smoke passes, existing suite green.

### 3.1 Documentation and guardrails
- [x] Production/scaling document with target architecture and SLOs
- [x] Request-body cap before JSON parsing (`guardrails.max_request_bytes`)
- [x] LLM timeout/retry settings passed to hosted clients
- [x] Readiness checks the conversation store
- [x] Dedicated `NullPool` engine for Postgres advisory locks
- [ ] Document operational runbooks (provider outage, DB failover, index rollback)

### 3.2 Authentication and tenancy
- [x] Add OIDC/JWT verification dependency (`blackbox/api/auth.py`)
- [x] Roles `viewer` / `operator` / `admin`; admin-only ingest/eval
- [x] Add `tenant_id` to conversations and turns (lightweight migration)
- [x] Tenant-scoped repository methods; cross-tenant reads return 404
- [ ] Mandatory tenant predicate in retrieval filters; reject empty tenant
- [ ] Postgres RLS policies as defense in depth
- [x] Authorization matrix tests (cross-tenant read/write/ingest → 403/404)
- [ ] Audit events for access, ingest, eval and admin actions
- [ ] Delete/rotate tenant credentials and offboarding

### 3.3 Persistence
- [x] Add Alembic (dependency, `alembic.ini`, baseline revision, upgrade/downgrade test)
- [ ] SQLAlchemy 2.0 `Mapped` models; remove ad-hoc `ALTER TABLE`
- [x] Indexes on `tenant_id` and `conversation_id` in the baseline migration
- [ ] JSONB + partition plan for turns/traces
- [ ] Migration check job in CI; rollback tested in a deployed environment
- [ ] Retention/deletion API and tenant-offboarding job

### 3.4 Shared state and concurrency
- [x] Redis fixed-window rate limiter with in-process fallback
- [x] `Idempotency-Key` support on `/api/chat` (final response replay)
- [ ] In-flight idempotency dedupe for simultaneous identical requests
- [x] Shared keys tenant-scoped for chat idempotency
- [ ] Max concurrent turns per tenant/pod; backpressure with `Retry-After`
- [ ] Verify lock release on SSE disconnect and pod shutdown
- [ ] Multi-pod Compose/kind smoke: two API replicas, one conversation

### 3.5 LLM resilience
- [ ] Async provider interface (`acomplete`) with sync wrappers for evals
- [ ] Retry with jitter, circuit breaker, per-tenant/provider semaphore
- [ ] Explicit token/cost quotas per tenant
- [ ] Provider 429/5xx/timeout tests with a fake provider

---

## 4. Phase 2 — Retrieval migration

Exit gate: eval parity (MRR ≥ 0.9214 or documented baseline), tenant filters proven, p95
within SLO at target load.

- [ ] `DenseStore` / `SparseStore` protocols
- [ ] Qdrant adapter (async, payload filters, batch upsert, snapshots)
- [ ] OpenSearch adapter (async, BM25, filter compilation, index aliases)
- [ ] Neutral metadata-filter AST; compile per store
- [ ] Mandatory tenant clause injected after compilation
- [ ] Concurrent dense/sparse legs (`asyncio.gather`)
- [ ] Keep score trail, fusion, rerank and eval harness unchanged
- [ ] Versioned indexes + `chunks-current` alias; staged ingest and atomic cutover
- [ ] Shadow indexing and dual-read comparison
- [ ] Eval parity gate wired into CI
- [ ] Remove Chroma/BM25 runtime dependency after cutover (rollback window documented)
- [ ] Benchmark Qdrant+OpenSearch vs OpenSearch-only; record decision

## 5. Phase 3 — Async runtime and jobs

- [ ] Async graph nodes and LangGraph execution
- [ ] Async retrieval stores + provider clients end-to-end
- [ ] Idempotent chat (Redis result replay)
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
- [ ] GitHub Actions: lint, mypy, tests, coverage, image scan/SBOM, migration check
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
- [ ] Docker image build in CI (currently unverified)
- [ ] Load test script (k6/Locust) committed
- [ ] PII redaction and query-log policy
- [ ] Security headers and dependency scanning
- [ ] Update README quick start for production mode (auth + external stores)

---

## 9. Decision log

| Date | Decision | Status | Notes |
|---|---|---|---|
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

## 12. Update protocol

1. Move items between `⬜`, `◐`, `✅` and add a one-line note with the commit/PR.
2. Add a row to the change log; never rewrite past rows.
3. Add new work under the correct phase; add a new phase only for a genuinely new workstream.
4. Record architecture decisions in the decision log and mirror major ones in
   `docs/scaling-and-production-readiness.md`.
5. Keep the status dashboard consistent with the checklists; CI/test counts must be current.
