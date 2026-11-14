# Blackbox Development Tracker

Living document for all work beyond the original assignment. Update this file with every
meaningful change so the repository always shows what is done, what is in flight, and what is
next.

- **Architecture and production contract:** [`scaling-and-production-readiness.md`](scaling-and-production-readiness.md)
- **Completed assignment phases:** [`phase-wise-plan.md`](phase-wise-plan.md)
- **Open bugs & scaling gaps (audit):** [`code-audit-findings.md`](code-audit-findings.md)
- **Status legend:** ✅ done · ◐ in progress · ⬜ not started · ⚠ blocked

Last updated: 2026-11-14

---

## 1. Baseline

| Item | State |
|---|---|
| Original assignment phases 0–9 | ✅ complete |
| Tests | 587 collected (575 passing, 12 skipped: live-cluster OpenSearch + opt-in Postgres/Redis), `ruff` clean, `mypy` clean, coverage 82.7% (gate 80%) |
| Runtime | FastAPI + SSE, LangGraph corrective RAG, three-layer memory, trace viewer |
| Auth | JWT/RBAC + tenant-scoped conversations implemented; dev-disabled by default |
| Retrieval | local Chroma + in-process BM25 (single node) behind the `SparseStore` protocol |
| Stores | Postgres/SQLite conversation + entity store |
| Deployment | Docker Compose (two-replica smoke) + Helm chart (`deploy/helm/blackbox`) + AWS Terraform reference (`deploy/terraform`) |
| Observability | Prometheus `/metrics` + recording/alert rules; OTel/Grafana/Loki/Tempo pending |
| Secrets | External Secrets example consuming Terraform-managed Secrets Manager entries; no static keys in pods |
| Production target | Kubernetes, org-wide, multi-region, strict tenant isolation |

## 2. Current status (2026-11-12)

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

**Audit remediation — complete (all nine findings fixed).**
- The code audit found one critical defect and eight scaling and quality gaps. All nine are
  fixed; see [`code-audit-findings.md`](code-audit-findings.md) for the analysis and the
  resolution of each one.
- Correctness: the SSE token stream no longer exposes an answer before the ground check has
  run; retrieval timings are per-request rather than shared; the budget endpoint reports the
  request that was actually sent, and the input ceiling now covers the whole prompt.
- Concurrency and scaling: the graph nodes are async, so a hosted LLM round trip no longer
  occupies a worker thread; the dense and sparse retrieval legs run concurrently; the worker
  pool is bounded by configuration.
- Operational: ingest and eval answer `202 {job_id}` and run on a background worker instead
  of occupying a request worker and refusing further work.
- Quality: `mypy` reports no issues across the package and CI gates on it.

**Phase 3 — complete (async runtime and jobs).**
- Async retrieval stores (`asearch` on the protocols and every adapter), with the hybrid
  retriever awaiting both legs concurrently.
- SSE cancellation: a client disconnect cancels the graph generator, releases the turn
  lock, tenant slot and idempotency key, and persists no partial turn.
- Shared job store: Redis-backed job records and pending queue, a handler registry, and a
  dedicated worker process (`python -m blackbox.worker`). Ingest, eval and retention are
  submit-by-params jobs; closures stay local-only by design.
- Staged ingest: a complete new versioned OpenSearch index is built, validated (documents,
  chunks, tenant coverage) and then published by an alias swap. Any failure drops the new
  version and leaves readers on the previous one.
- Trace store split: turns are archived to file/S3 (`FileTraceArchive`, `S3TraceArchive`)
  before retention deletes them; `/trace` is paginated and can merge the archived window.
- 454 tests green (9 skipped without an OpenSearch cluster), `ruff` clean, `mypy` clean.

**Phase 4 — complete (reference implementation; infra-bound validation pending).**
- **Helm chart** (`deploy/helm/blackbox`): API and worker Deployments, HPA (CPU by default,
  custom metrics supported), KEDA `ScaledObject` on Redis queue depth, PodDisruptionBudgets,
  topology spread, startup/readiness/liveness probes, a `pre-install/pre-upgrade` migration
  Job, optional SSE-correct Ingress, NetworkPolicy templates, ServiceMonitor and a Helm test.
  `helm lint` and `helm template` are clean for both the default and prod-example values.
- **Image hardening:** the Dockerfile now creates uid/gid 10001 and runs unprivileged; it
  ships `alembic.ini` and `alembic/` so the migration Job works.
- **Metrics:** dependency-free Prometheus registry (`blackbox/observability/metrics.py`) and
  `/metrics`; HTTP route-template labels, SSE active gauge, retrieval stage histograms, LLM
  calls/duration/tokens/errors, job queue depth. `/metrics` is rate-limit exempt and meant to
  be NetworkPolicy-restricted (Prometheus cannot carry a user JWT).
- **Alerting:** recording rules plus multi-window error-budget burn and SLO alerts
  (`deploy/observability/prometheus-rules.yaml`).
- **Tracing:** optional OpenTelemetry spans (`blackbox/observability/tracing.py`) across
  HTTP requests, graph nodes, retrieval, LLM calls, chat turns and the turn write. The
  facade is a no-op unless `OTEL_ENABLED=true` and the `otel` extra is installed, so the
  default install stays dependency-light. The HTTP span is pure-ASGI so it covers the whole
  SSE stream; span names and attributes use route templates, and `trace_id` is bound into
  structlog context for log/trace correlation. OTLP/HTTP goes to a collector (the
  deployment seam), not directly to a storage backend.
- **Dashboards and stack configs** (`deploy/observability`): a Grafana overview dashboard
  pinned to the exported metric names, Prometheus/Loki/Tempo datasource provisioning,
  TraceQL/log panels, an OTel Collector pipeline (memory limiter + batch + Tempo exporter),
  single-binary Tempo/Loki references and Promtail values that extract `trace_id`.
- **GitOps delivery reference** (`deploy/gitops`): Argo CD Application over the chart, an Argo
  Rollouts canary with SLO analysis and canary Service, and a README covering promotion,
  abort, blue-green, migration ordering and rollback. The chart gains `api.enabled` so the
  Rollout can own the API pods without conflicting with the Deployment.
- **Load and failure-injection assets**: `scripts/load_test.py` (async, standard-library-only
  SSE load harness reporting TTFT/turn percentiles, error rate and throughput) and
  `deploy/chaos/*` (Chaos Mesh pod kill/failure and network delay experiments with success
  criteria); `tests/test_load_test.py` and `tests/test_chaos_assets.py`.
- **Quotas and runbooks**: global per-tenant LLM/token quotas (`blackbox/api/quotas.py`,
  Redis-backed with local fallback, wired into chat admission and turn recording),
  `docs/pen-test-checklist.md` and `docs/secret-rotation-runbook.md`.
- **Synthetic probes and on-call runbook**: `scripts/smoke_probe.py` and
  `docs/on-call-runbook.md`.
- **CI:** coverage gate (80%, currently 81.2%), chart lint/render with kubeconform, Docker
  image build, Trivy scan and SPDX SBOM.
- **Terraform reference** (`deploy/terraform`): a self-contained AWS root module — VPC with
  flow logs, EKS (private endpoint by default, CMK-encrypted Secrets, managed node groups,
  taints for workers), Aurora PostgreSQL 16 with an RDS-managed master password, ElastiCache
  Redis with TLS + `noeviction`, a versioned KMS-encrypted S3 trace archive, KMS keys and
  Secrets Manager. IRSA roles are split: the app can write traces but not read secrets, the
  External Secrets controller can read secrets but has no S3 access. Validated with
  `terraform fmt`/`validate` and a Trivy config scan (no HIGH/CRITICAL); not applied, because
  there is no AWS account in this environment.
- **External Secrets example** (`deploy/external-secrets`): assembles the chart's
  `existingSecret` from the Terraform secrets plus the RDS-managed credential, including the
  `REDIS_PASSWORD` key KEDA's scaler needs.
- **Bug fixed:** `python -m blackbox.worker` imported the empty routes package, not the
  operations module, so a fresh worker registered no job handlers. It now imports
  `blackbox.api.routes.operations`; a subprocess test proves every job kind is registered.
- 490 tests collected (481 passing, 9 skipped: live-cluster OpenSearch tests),
  `ruff`/`mypy` clean; coverage 81.2%.

**Phase 5 — complete as a reference (2026-11-14).**
- Multi-region application behaviour: `tenant_regions` + `TenantRegionResolver`, 421
  write pinning with `X-Blackbox-Home-Region`, `DATABASE_READ_URL` read replica,
  `pool_pre_ping`/pool sizing, and the RLS `app.tenant_id` GUC wired per transaction.
- Per-region retrieval: Qdrant replication/shard config, OpenSearch replica count +
  `OPENSEARCH_ALIAS`, region-scoped index names, and a rebuild-from-documents path.
- Regional Redis: `JobQueue.redrive()` on worker startup plus queue-loss-risk and
  cache-fallback metrics.
- Cost: per-node trace usage, `blackbox/observability/cost.py`, `/api/conversations/:id/cost`
  and `/api/admin/cost`, token/call/dollar quotas, `docs/cost-model.md`.
- Concurrency and hardening: global per-tenant slots (`blackbox/api/admission.py`),
  PII redaction + query-log policy (`blackbox/pii.py`), security headers, `pip-audit` and
  `gitleaks` in CI, provider contract tests and opt-in store integration tests.
- DR: failover/restore/tenant-move drills with measured RTO/RPO (`docs/dr-drill-results.md`),
  `docker-compose.dr.yml`, `docs/failover-runbook.md`, `docs/tenant-move-runbook.md`.
- Chaos: AZ/store/provider/region experiments and a machine-checkable `chaos_verify.py`.
- Infra: gated Terraform multi-region layer (Aurora/Redis global, KMS/secret replicas,
  S3 CRR, Route 53 failover) and a GitOps ApplicationSet with per-region overlays;
  `terraform fmt`/`validate` clean (the multi-region code paths are validated even when
  the flag is false), not applied.

**Next (infra-bound):** apply the multi-region Terraform layer to a real account, run the
drills against live regional endpoints, and run the load/chaos suites for measured
capacity. Everything code-doable is complete: global concurrency, dollar budgets, PII and
query-log policy, security headers, dependency/secret scanning, provider contract tests
and opt-in store integration tests.
**Not started:** nothing new; the assignment and production phases 0–5 are implemented.

### Resume point (next session)

State: **587 tests collected (575 passing, 12 skipped: live-cluster OpenSearch and the
opt-in Postgres/Redis integration suites)**; `ruff` clean, `mypy` clean (109 source
files), coverage 82.7% (gate 80%).
Phase 5 is implemented as a reference: application-level multi-region behaviour and
drills run offline; the Terraform multi-region layer validates but was not applied
(no AWS account), and Helm was not available locally this session (CI lints/renders it).
Phase 1 (control plane), the audit remediation, Phase 2 (retrieval migration and tenant
isolation), Phase 3 (async runtime and jobs) and the **deployment, observability and
infrastructure slices of Phase 4 are complete**.

**Phase 4 delivered:**
1. ✅ Metrics — `blackbox/observability/metrics.py`, `/metrics` route, HTTP/SSE/retrieval/LLM/
   queue instrumentation; tests in `tests/test_metrics.py`.
2. ✅ Helm chart — `deploy/helm/blackbox` (API, worker, migration Job, HPA, KEDA, PDB,
   NetworkPolicy, Ingress, ServiceMonitor, probes, topology spread, per-component README).
3. ✅ Image hardening — unprivileged uid/gid 10001; `alembic.ini` + migrations in the image.
4. ✅ Alerting — `deploy/observability/prometheus-rules.yaml` (recording rules, multi-window
   burn, latency/retrieval/LLM/backlog alerts) and metric contract in its README.
5. ✅ CI — coverage gate, chart lint/render + kubeconform, image build, Trivy, SPDX SBOM.
6. ✅ Worker registration fix — `blackbox/worker.py` now imports the operations module;
   `tests/test_worker_entrypoint.py` guards it in a fresh interpreter.
7. ✅ Terraform reference — `deploy/terraform` (VPC/flow logs, EKS, Aurora, ElastiCache, S3
   traces, KMS, Secrets Manager, IRSA for app + External Secrets); `terraform validate` clean,
   Trivy config clean at HIGH/CRITICAL; CI job added. Not applied (no AWS account).
8. ✅ External Secrets example — `deploy/external-secrets` builds the chart's
   `existingSecret` from Terraform's secrets and documents the rotation/restart step.
9. ✅ CI Terraform job — `terraform fmt -check`, `init -backend=false`, `validate`, Trivy config.
10. ✅ OpenTelemetry tracing — optional `otel` extra; no-op facade; spans for HTTP (ASGI,
    stream-covering), graph nodes, retrieval, LLM, chat turn and DB write; trace id in logs;
    chart/values and Dockerfile wiring; `tests/test_tracing.py` (including a fake-SDK path).
11. ✅ Dashboards and stack configs — Grafana overview dashboard over the metric contract,
    Prometheus/Loki/Tempo datasource provisioning, OTel Collector/Tempo/Loki references and
    Promtail values; `tests/test_observability_assets.py` pins panel queries to the registry.
12. ✅ GitOps delivery reference - Argo CD Application, Argo Rollouts canary with SLO analysis,
    canary Service and README; chart gains `api.enabled` so the Rollout owns the API pods;
    `tests/test_gitops_assets.py`.
13. ✅ Load and failure-injection assets — `scripts/load_test.py` and `deploy/chaos/*`;
    `tests/test_load_test.py` and `tests/test_chaos_assets.py`.
14. ✅ Global per-tenant LLM/token quotas + pen-test checklist + secret-rotation runbook;
    `blackbox/api/quotas.py`, `tests/test_quotas.py`, `docs/pen-test-checklist.md`,
    `docs/secret-rotation-runbook.md`.
15. ✅ Synthetic probe and on-call runbook — `scripts/smoke_probe.py`,
    `docs/on-call-runbook.md`, `tests/test_smoke_probe.py`.

**Next: finish Phase 4, in this order.**
1. Terraform: OpenSearch/Qdrant once the backend benchmark decides the shape; WAF at the edge.

**Carried over (infra-bound):**
- Qdrant+OpenSearch vs OpenSearch-only benchmark — `scripts/benchmark_backends.py` needs
  live clusters; record the decision in §9.
- Terraform has not been applied to a real account; the first `terraform plan` still needs a
  review (Aurora engine availability per region, account quotas, existing EKS add-ons).
- Global per-tenant LLM/token quotas — Phase 4.
- Runtime warm-up at pod start (readiness currently passes before the retriever/LLM are
  built; first chat pays cold start).
- Summary compaction remains inline in the turn (deliberate: it mutates the memory the
  same turn reads).

**Commands:**
```powershell
.\.venv\Scripts\python.exe -m pytest -q --cov=blackbox --cov-fail-under=80
.\.venv\Scripts\python.exe -m ruff check blackbox tests scripts alembic
.\.venv\Scripts\python.exe -m mypy blackbox
helm lint deploy/helm/blackbox -f deploy/helm/blackbox/values-prod.example.yaml
helm template blackbox deploy/helm/blackbox --namespace blackbox | less
cd deploy\terraform; terraform fmt -check -recursive; terraform init -backend=false; terraform validate
trivy config --severity HIGH,CRITICAL deploy\terraform
```

**Commands (Docker / live cluster):**
```bash
.\.venv\Scripts\python.exe scripts\multi_pod_smoke.py   # Docker; shared Postgres + Redis
.\.venv\Scripts\python.exe -m blackbox.worker           # shared-queue consumer (needs REDIS_URL)
```

**To exercise the OpenSearch backend locally:**
```bash
docker run -d --name blackbox-opensearch -e discovery.type=single-node \
  -e DISABLE_INSTALL_DEMO_CONFIG=true -e DISABLE_SECURITY_PLUGIN=true \
  -e OPENSEARCH_JAVA_OPTS="-Xms512m -Xmx512m" -p 9200:9200 \
  opensearchproject/opensearch:2.19.1
# then: set retrieval.sparse_backend=opensearch in config.yaml and
# OPENSEARCH_HOSTS=http://127.0.0.1:9200 in the environment
```

**Open decision (carried into Phase 4):** Qdrant+OpenSearch vs OpenSearch-only
(benchmark with the eval harness once live clusters exist). Tenant corpus strategy is
settled: one shared collection/index with mandatory tenant payloads.

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
| Qdrant/OpenSearch migration | 2 | ✅ | tenant filters, RLS, Qdrant adapter, versioned indexes/alias, shadow/dual-read, offboarding, retention, JSONB/partition, eval gate; only the live-cluster benchmark and global quotas remain |
| Async graph + workers | 3 | ✅ | async graph/providers/stores; SSE cancellation; shared Redis queue + handler registry + worker process; graceful drain |
| Staged ingest + alias swap | 3 | ✅ | validate documents/chunks/tenant coverage before `swap_alias`; abort keeps the old version |
| Trace store split | 3 | ✅ | Postgres recent + file/S3 archive; paginated `/trace`; archive-before-prune retention (sync or queued) |
| Kubernetes/Helm/Terraform | 4 | ✅ | Helm chart complete; Terraform AWS reference complete and validate/scan-clean (OpenSearch/WAF/Route 53 optional); infra-bound validation pending |
| Observability stack | 4 | ✅ | Prometheus `/metrics` + recording/SLO alerts + optional OTel traces + Grafana dashboard/provisioning + collector/Tempo/Loki references + synthetic probe done; error tracking optional |
| CI/CD + security scanning | 4 | ✅ | coverage gate, chart lint/render, image build, Trivy scan, SPDX SBOM, GitOps/canary reference, `pip-audit` + `gitleaks` |
| Multi-region + DR | 5 | ✅ | tenant-pinned writes (421), RLS GUC, read replica, per-region retrieval + rebuild, Redis redrive, tenant move, measured drills, gated Terraform global layer + GitOps ApplicationSet; live-region drills pending |
| Cost model + budgets | 5 | ✅ | per-node trace usage, `/api/conversations/:id/cost`, `/api/admin/cost`, token/call/dollar quotas, `docs/cost-model.md` |
| Global concurrency + hardening | 4/5 | ✅ | Redis slot counter + local fallback; PII/query-log policy; security headers; pip-audit + gitleaks in CI; provider contract + opt-in store integration tests |
| Load/chaos suites | 5 | ◐ | AZ/store/provider/region chaos manifests + `chaos_verify.py`; target-SLO run pending |

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
- [x] Mandatory tenant predicate in retrieval filters; reject empty tenant — **Phase 2** (landed)
- [x] Postgres RLS policies as defense in depth — migration 0002 enables RLS on conversations
  and conversation_turns with per-tenant policies keyed on `app.tenant_id` (Postgres only) — **Phase 2**
- [x] Delete/rotate tenant credentials and offboarding — **Phase 2** (`DELETE /api/admin/tenants/{id}`)

### 3.3 Persistence
- [x] Add Alembic (dependency, `alembic.ini`, baseline revision, upgrade/downgrade test)
- [x] Remove ad-hoc `ALTER TABLE` DDL; local/test bootstrap uses `create_all` only
- [x] Indexes on `tenant_id` and `conversation_id` in the baseline migration
- [x] Migration check job in CI (`upgrade head` + `downgrade base`)
- [x] SQLAlchemy 2.0 `Mapped` models; JSONB + partition plan for turns/traces — **Phase 2** (landed)
- [x] Retention/deletion API and tenant-offboarding job — **Phase 2** (landed)

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
- [x] Async `acomplete` provider interface — audit finding 5
- [x] Async graph/providers with no threadpool hops — audit finding 5
- [x] Explicit token/call quotas per tenant — `blackbox/api/quotas.py` (Phase 4)
- [x] Explicit dollar budget per tenant — `TENANT_BUDGET_USD_PER_DAY`, recorded from
      `turn_cost` and checked at chat admission (`budget_usd` reason)

---

## 4. Phase 2 — Retrieval migration and data-plane isolation

Exit gate: eval parity (MRR ≥ 0.9167 or documented baseline), tenant filters proven, p95
within SLO at target load.

- [x] `DenseStore` / `SparseStore` protocols — audit finding 6
- [x] Mandatory tenant clause injected after compilation; reject empty tenant — tenant_id stamped
  into chunk metadata at ingestion (Chroma + OpenSearch), MetadataFilter carries tenant_id,
  compilation (Chroma where, BM25 matches, OpenSearch clauses) always includes it, retriever
  resolves tenant per call and overrides any caller-supplied value, cross-tenant retrieval
  tests added (tests/test_tenant_isolation_retrieval.py)
- [x] Qdrant adapter (payload filters, batch upsert, snapshots, in-memory tested)
  blackbox/ingestion/qdrant_vectorstore.py + optional `vector` extra landed; wiring into
  build_default_retriever pending
- [x] OpenSearch adapter (async, BM25, filter compilation) — audit finding 6; run against a live
  OpenSearch 2.19 cluster, and the ingestion pipeline populates its index
- [x] Neutral metadata-filter AST; compile per store — audit finding 6
- [x] Postgres RLS policies as defense in depth — migration 0002 enables RLS on conversations
  and conversation_turns with per-tenant policies keyed on `app.tenant_id` (Postgres only)
- [x] Concurrent dense/sparse legs (`asyncio.gather`) — audit finding 5
- [x] Keep score trail, fusion, rerank and eval harness unchanged — the score trail, fusion and
  rerank are untouched; the eval harness gained a tie-break sweep and a fix for a sweep that
  left the retriever's config mutated
- [x] The two sparse backends measure identically — the sparse leg now scores with Lucene's
  formula in both adapters, so the OpenSearch backend reproduces the in-process numbers exactly
  (dense 0.8548 / sparse 0.8714 / hybrid 0.9167 / hybrid+rerank 0.8857)
- [x] Versioned indexes + alias cutover - alias mode in OpenSearch adapter (opt-in), staged via create_versioned/swap_alias
- [x] Shadow indexing and dual-read comparison — ShadowingRetriever (dual-read) + shadow_store dual-write in ingestion pipeline
- [x] Eval parity gate wired into CI — `--min-mrr` flag on retrieval_eval, CI step added
- [x] Remove Chroma/BM25 runtime dependency after cutover — rollback window documented in scaling-and-production-readiness.md; dev adapters retained as rollback path
- [ ] Benchmark Qdrant+OpenSearch vs OpenSearch-only; record decision (script: scripts/benchmark_backends.py; needs live clusters - deferred)
- [x] SQLAlchemy 2.0 `Mapped` models — already in place (ConversationRow/TurnRow use Mapped + mapped_column)
- [x] JSONB conversion migration (0003) — Postgres only; models stay portable JSON
- [x] Partition plan for turns/traces — partition_month column + index + migration 0004 (range partitioning ready)
- [x] Retention/deletion APIs beyond tenant offboarding — delete_older_than + admin endpoint
- [x] Tenant offboarding (conversations + turns) via `DELETE /api/admin/tenants/{tenant_id}` and `ConversationRepository.delete_tenant`; entity/index payloads are store-wide today
- [x] Global per-tenant LLM/token quotas — `blackbox/api/quotas.py` (Redis-backed, local fallback)

## 5. Phase 3 — Async runtime and jobs

- [x] Async graph nodes and LangGraph execution — audit finding 5
- [x] Async retrieval stores + provider clients end-to-end — `asearch` on the `DenseStore`/`SparseStore` protocols and on `DenseRetriever`, `SparseRetriever` and `OpenSearchSparseStore`; `HybridRetriever` awaits both legs concurrently
- [x] SSE cancellation — `stream_with_disconnect` pumps the graph into a queue while polling `request.is_disconnected()`; a disconnect cancels the producer, closes the graph generator, releases the turn lock/tenant slot/idempotency key and persists no partial turn (`tests/test_sse_cancellation.py`)
- [x] Graceful shutdown/drain — lifespan stops job workers and shuts the thread pool down (`tests/test_graceful_shutdown.py`)
- [x] Shared queue backend — `RedisJobStore` + handler registry (`register_handler`); API submits `kind + params`, any replica or `python -m blackbox.worker` executes it; closures remain local-only and fail with a clear reason if another pod wins them (`tests/test_shared_jobs.py`)
- [x] `202 + job_id` ingest/eval APIs with status endpoints — audit finding 8
- [x] Staged ingest with validation and alias swap — builds a new versioned index, validates documents/chunks/tenant coverage, swaps the alias only on success, and aborts back to the previous version on any failure (`blackbox/ingestion/staged.py`, `tests/test_staged_ingest.py`)
- [x] Trace store split: Postgres recent + file/S3 archive — `FileTraceArchive`/`S3TraceArchive` (gzipped JSONL batches), archive-before-prune (`blackbox/api/retention.py`)
- [x] Paginated `/trace` (`limit`, `offset`, `include_archive`) and retention jobs — synchronous run or `background=true` (202) on the shared queue; exact turn ids are deleted only after the archive write succeeds

Scope note: summary compaction stays inline in the chat turn. It mutates the
conversation memory that the same turn is about to read, so queueing it would race the
turn; the expensive, independent jobs (ingest, eval, retention) are the ones on the
queue.

## 6. Phase 4 — Kubernetes and operations

- [x] Helm chart: API, workers, HPA/KEDA, PDB, NetworkPolicies
- [x] Terraform: VPC/EKS, Aurora, ElastiCache, S3 trace archive, KMS, Secrets Manager, IRSA
- [x] Terraform: OpenSearch/WAF/Route 53 shapes behind flags (backend choice still awaits the benchmark)
- [x] External Secrets example for the chart's `existingSecret` + rotation notes
- [ ] SQS/MSK (not needed: the queue is Redis; revisit only if the job queue outgrows it)
- [x] Ingress SSE config: buffering off, idle timeout, body cap, TLS
- [x] Startup/readiness/liveness probes — startup `/health`, readiness `/health/ready`, liveness `/health`
- [x] Runtime warm-up at pod start — lifespan calls `ensure_runtime()`; readiness reports `runtime`
- [x] Prometheus metrics + `/metrics` (HTTP, SSE, retrieval, LLM, queue)
- [x] Prometheus recording rules and SLO/burn alerts (`deploy/observability`)
- [x] OpenTelemetry traces across HTTP → graph node → retrieval → LLM → DB (optional
      `otel` extra; no-op when disabled)
- [x] Grafana overview dashboard + Prometheus/Loki/Tempo provisioning
- [x] OTel Collector, Tempo, Loki and Promtail configs (`deploy/observability`)
- [x] Synthetic probes, optional error tracking and on-call runbooks
- [x] GitHub Actions: lint, tests, migration check
- [x] CI: coverage (80% gate), image scan/SBOM (Trivy + Syft), image build, chart validation (mypy is done)
- [x] CI: `terraform fmt -check`, `init -backend=false`, `validate` and Trivy config scan
- [x] Migration Job (Helm pre-install/pre-upgrade `alembic upgrade head`)
- [x] GitOps + canary/blue-green delivery (`deploy/gitops`: Argo CD Application, Argo Rollouts
      canary with SLO analysis, canary Service, README)
- [x] Load tests at target concurrency; failure injection (assets + success criteria; run pending)
- [x] Pen-test checklist and secret rotation (`docs/pen-test-checklist.md`, `docs/secret-rotation-runbook.md`)
- [x] Synthetic probe and on-call runbook (`scripts/smoke_probe.py`, `docs/on-call-runbook.md`)
- [x] Global per-tenant LLM/token quotas (Redis-backed, fail open)
- [x] Pen-test checklist and secret rotation (`docs/pen-test-checklist.md`,
      `docs/secret-rotation-runbook.md`)

### Remaining to close Phase 4

- [x] **Terraform backend shapes**: optional OpenSearch domain, WAFv2 web ACL and Route 53
      zone added behind feature flags (`enable_opensearch`, `enable_waf`, `enable_route53`);
      OpenSearch vs Qdrant shape still follows the Phase 2 benchmark, but the module is
      ready.
- [x] **Runtime warm-up at pod start**: lifespan now calls `ensure_runtime()` before serving;
      `/health/ready` reports `runtime` state and returns 503 when warm-up was attempted but
      failed.
- [x] **Error tracking**: optional Sentry init from `SENTRY_DSN` (fail-open; extra `sentry`).
- [x] **Per-region synthetic probes**: `deploy/probes/synthetic-probe-cronjob.yaml`.
- [x] **Global per-tenant concurrency**: `blackbox/api/admission.py` — Redis slot counter
      with a lease TTL and a local fallback, enforced alongside the per-pod fast check.
- [x] **Dependency and secret scanning in CI**: `pip-audit` in the test job and a
      `gitleaks` job in `.github/workflows/ci.yml`.
- [ ] **Terraform apply**: first `terraform plan`/apply against a real AWS account.
- [ ] **Live-cluster backend benchmark**: run `scripts/benchmark_backends.py` and record
      the OpenSearch-only vs Qdrant+OpenSearch decision in §9.
- [ ] **Run the load/chaos suites** against a staging cluster and record measured capacity
      vs the provisional SLOs.

## 7. Phase 5 — Multi-region, DR, cost and chaos

- [x] Tenant home-region routing — `tenant_regions` is authoritative, `TenantRegionResolver`
  caches it, `REGION_WRITE_PINNING` returns 421 + `X-Blackbox-Home-Region` for a non-home
  conversation access (`blackbox/api/regions.py`, `tests/test_tenant_region_pinning.py`)
- [x] Regional Postgres with cross-region DR; failover runbook — read replica via
  `DATABASE_READ_URL`, `pool_pre_ping`/pool sizing, RLS `app.tenant_id` GUC,
  `docs/failover-runbook.md`, gated Terraform Aurora global + DNS failover
- [x] Qdrant/OpenSearch per-region replication and rebuild path — Qdrant replication/shard
  config, OpenSearch replica count + `OPENSEARCH_ALIAS`, region-scoped index names,
  `blackbox/ingestion/rebuild.py` + `scripts/rebuild_region_indexes.py`
- [x] Redis regional, non-authoritative — `JobQueue.redrive()` re-queues stranded jobs on
  worker startup, `blackbox_job_queue_loss_risk` and `blackbox_cache_fallback_active` metrics
- [x] Tenant move procedure — `export_tenant`/`import_tenant`, `move_tenant`,
  `scripts/move_tenant.py`, `docs/tenant-move-runbook.md`; idempotent and verified
- [x] RTO/RPO drills with measured results — `scripts/dr_drill.py`, `scripts/restore_drill.py`,
  generated `docs/dr-drill-results.md`, `docker-compose.dr.yml`
- [x] Cost model per tenant/conversation; budgets and quotas — per-node trace usage,
  `blackbox/observability/cost.py`, `/api/conversations/{id}/cost`, `/api/admin/cost`,
  daily token/call quotas plus a dollar budget (`TENANT_BUDGET_USD_PER_DAY`),
  `docs/cost-model.md`
- [x] Chaos suite: pod/AZ/store/provider failures — `deploy/chaos/{az,store,provider,region}*.yaml`
  and `scripts/chaos_verify.py`

---

## 8. Cross-cutting backlog

- [x] Fix remaining mypy errors; CI type-check gate — audit finding 9
- [x] Coverage threshold and coverage report in CI — 80% gate, currently 80.6%
- [x] Provider contract tests (request shape + response mapping with injected stub clients)
- [x] Postgres/Redis integration tests — opt-in via `POSTGRES_TEST_URL`/`REDIS_TEST_URL`
      (`tests/test_integration_stores.py`); skipped in the default offline suite
- [x] Docker image build in CI (local two-replica smoke passes)
- [x] Load test script committed — `scripts/load_test.py` (standard library, SSE-aware)
- [x] PII redaction and query-log policy — `blackbox/pii.py`, `PII_REDACTION_ENABLED`,
      `QUERY_LOG_MODE` (hash default), `docs/pii-and-query-logging.md`
- [x] Security headers and dependency scanning — pure-ASGI `SecurityHeadersMiddleware`,
      `pip-audit` + `gitleaks` in CI
- [x] README quick start for production mode (auth, external stores, migrations)

---

## 9. Decision log

| Date | Decision | Status | Notes |
|---|---|---|---|
| 2026-11-10 | Phase 1 closes the control plane; retrieval tenant predicates + RLS are Phase 2 | Accepted | Stores are replaced in Phase 2, so the filter is proven there |
| 2026-11-10 | Per-pod tenant admission control; global quotas later | Provisional | Redis global counters are Phase 2 |
| 2026-11-10 | Kubernetes, AWS EKS reference, org-wide target | Accepted | Portable via Helm |
| 2026-11-10 | Keep application-side RRF fusion | Accepted | Preserves measured 0.9167 MRR |
| 2026-11-10 | Qdrant (dense) + OpenSearch (sparse) | Provisional | Revisit after Phase 2 benchmark |
| 2026-11-10 | Postgres advisory locks on dedicated NullPool engine | Accepted | Avoids SSE holding pooled connections |
| 2026-11-10 | Tenant-pinned conversation writes across regions | Accepted | Avoids cross-region write conflicts |
| 2026-11-10 | Traces split to ClickHouse/S3 | Superseded | Resolved 2026-11-12: archive is file/S3 gzipped JSONL, not ClickHouse (see below) |
| 2026-11-10 | Provisional SLOs (2k SSE, 200 turns/s, 10M+ chunks) | Provisional | Replace with measured capacity |
| 2026-11-11 | Resolve the client IP from `X-Forwarded-For` only when the peer is a trusted proxy | Accepted | A spoofable header would make the rate limit trivial to bypass |
| 2026-11-11 | Background jobs run on a dedicated worker thread, not on the request's event loop | Accepted | A request's loop does not outlive the request |
| 2026-11-11 | The in-process BM25 index is a dev-only adapter behind `SparseStore` | Accepted | Chroma + BM25 stay the default and the rollback path until eval parity is green |
| 2026-11-11 | In-memory SQLite uses a static pool | Accepted | A `:memory:` database belongs to a connection, and SQLAlchemy gives each thread its own |
| 2026-11-11 | The sparse leg scores with Lucene's BM25 formula in both adapters | Accepted | `rank_bm25` uses a different inverse document frequency, so the dev adapter was not measuring the system the OpenSearch backend runs |
| 2026-11-11 | Equal BM25 scores are broken by document id, then chunk id | Accepted | The previous tie-break was the dense store's internal id order, which made the ranking backend-dependent |
| 2026-11-11 | Phase 2 gate re-baselined to MRR ≥ 0.9167 | Accepted | The old 0.9214 was measured with a scoring function the production backend does not use; the 0.0047 difference is six times smaller than the metric's tie-break sensitivity |
| 2026-11-12 | Data-plane tenant isolation is enforced in the stores, not only in the API | Accepted | Empty tenant is rejected, never "all tenants"; the retriever overwrites any caller-supplied tenant |
| 2026-11-12 | Trace archive is file or S3-compatible object storage, not ClickHouse | Accepted | Gzipped JSONL batches are append-only and need no cluster; ClickHouse remains an analytics option, not the archive of record |
| 2026-11-12 | Retention archives first and deletes only after the write succeeds | Accepted | Deleting by exact turn id avoids sweeping a turn written between the read and the delete |
| 2026-11-12 | Background jobs are described by `kind + params`, not closures, on a shared store | Accepted | A closure cannot travel through Redis; `python -m blackbox.worker` executes registered handlers from any replica |
| 2026-11-12 | Summary compaction stays inline in the turn | Accepted | It mutates the memory the same turn reads; queueing it would race the turn |
| 2026-11-12 | A staged ingest is always a full rebuild (force=True) | Accepted | Reusing the dedupe skip would leave the new index missing unchanged documents |
| 2026-11-13 | Metrics are dependency-free and label on the route template | Accepted | The exposition format is small and stable; a raw path (conversation id) would create unbounded time series |
| 2026-11-13 | `/metrics` is unauthenticated and NetworkPolicy-restricted | Accepted | Prometheus cannot present a user JWT; the monitoring namespace is the access boundary |
| 2026-11-13 | Migrations run as a Helm pre-install/pre-upgrade hook | Accepted | A failed migration aborts the release; the previous pods keep serving on the previous schema |
| 2026-11-13 | KEDA scales the worker on Redis pending-list length | Accepted | With a shared queue it is the only signal that sees work submitted by other pods; CPU is blind to a waiting worker |
| 2026-11-13 | Chart ships NetworkPolicy disabled by default | Accepted | The allowlist is cluster-specific; the prod example enables it with an explicit ingress/monitoring/store policy |
| 2026-11-13 | Container runs as uid/gid 10001 | Accepted | Matches PodSecurity restricted; the chart sets the same identity so no volume chown surprise |
| 2026-11-13 | CI coverage gate is 80% | Accepted | Current coverage is 81.2%; high enough to catch an untested module, low enough not to reward assertion-free tests |
| 2026-11-13 | Terraform reference is a flat root module, no registry modules | Accepted | The plan is reviewable in this repo, `validate` works offline, and a registry module's defaults would hide the security decisions |
| 2026-11-13 | Tracing is an optional extra behind a no-op facade | Accepted | Serving must not depend on a collector; the default install stays small and every call site is testable with a recording tracer |
| 2026-11-13 | OTLP/HTTP goes to a collector, not directly to Tempo | Accepted | The collector is the deployment seam: sampling, batching and backend choice change without an app release |
| 2026-11-13 | HTTP tracing is pure ASGI, not `BaseHTTPMiddleware` | Accepted | A `BaseHTTPMiddleware` span ends when `call_next` returns, which for SSE is before the answer is produced |
| 2026-11-13 | Dashboards are tested against the metric registry | Accepted | A panel built on a metric the app no longer exports renders silently; the test fails the dashboard instead |
| 2026-11-13 | Tempo/Loki/collector configs are single-binary references | Accepted | They prove the pipeline and validate offline; production uses the upstream charts with object storage |
| 2026-11-13 | Promtail ships only the Blackbox namespace | Accepted | The agent should not label every pod in the cluster; a broader rollout needs a scoped namespace list |
| 2026-11-13 | GitOps delivery keeps the chart as the platform layer | Accepted | The Rollout owns only the API pods; services, ingress, config and the migration hook stay chart-managed |
| 2026-11-13 | Canary analysis uses the recorded SLO metrics | Accepted | The same queries as the alert rules; a canary fails on the same signal an operator would page on |
| 2026-11-13 | Load harness is standard-library only | Accepted | No new dependency in the default install; CI and operators can run it anywhere Python runs |
| 2026-11-13 | Chaos experiments are namespace-scoped references | Accepted | They are runbooks in YAML form; nothing is applied by default |
| 2026-11-13 | Synthetic probe is standard-library only | Accepted | It runs from any CronJob or uptime checker without installing dependencies |
| 2026-11-13 | Global per-tenant quotas are Redis-backed with local fallback | Accepted | Shared budget across pods; fails open if Redis is down, matching rate limiting |
| 2026-11-13 | Quotas are enforced at chat admission and recorded after the turn | Accepted | Rejecting before the turn avoids spending; recording after captures real usage |
| 2026-11-13 | EKS API endpoint is private by default | Accepted | The API server is the highest-value target; a VPN or bastion costs less than a public endpoint |
| 2026-11-13 | Aurora uses the RDS-managed master password | Accepted | No credential in state or tfvars; pointing it at a CMK needs an extra grant for the RDS service role, documented as a follow-up |
| 2026-11-13 | Two IRSA roles: app (S3 traces) and External Secrets (read secrets) | Accepted | A compromised pod should not be able to read the database password just because it can archive traces |
| 2026-11-13 | Redis `maxmemory-policy = noeviction` | Accepted | The instance shares the job queue and idempotency keys with caches; eviction would silently drop work |
| 2026-11-13 | Terraform is validated, not applied | Accepted | There is no AWS account in this environment; the first plan needs a human review for regional engine versions and quotas |
| 2026-11-14 | Tenant→region mapping lives in `tenant_regions`, not the JWT | Accepted | The DB is the authority on failover; a token claim would let a stale client pin the wrong region |
| 2026-11-14 | A non-home conversation access returns 421 + `X-Blackbox-Home-Region`, not an in-app proxy | Accepted | The edge owns routing; the app never writes another region's database |
| 2026-11-14 | RLS is enforced via `set_config('app.tenant_id', …, true)` on transaction begin | Accepted | The 0002 policies read a GUC nothing set; enabling RLS without this would hide every row |
| 2026-11-14 | `pool_pre_ping` and bounded pool sizes are on by default | Accepted | A failover endpoint flip must heal, not surface a stale-connection error |
| 2026-11-14 | Cost is derived on read from per-node trace usage; there is no cost column | Accepted | Repricing and adding models need no migration, and historical turns are repriced correctly |
| 2026-11-14 | An unpriced model is reported, never counted as free | Accepted | A report that silently omits a model is worse than one that says it cannot price it |
| 2026-11-14 | Regional Redis loss loses queued work; `redrive()` recovers records that survived | Accepted | Pending lists are not durable; the DB/record index is the recoverable source |
| 2026-11-14 | DR drills run against a local SQLite/Compose harness; Terraform extends the reference | Accepted | No AWS account here; the harness proves the measurement and procedure, and the same code measures live endpoints |
| 2026-11-14 | Retrieval index names are region-scoped when `region_suffix` is on | Accepted | Two regions sharing a physical index would let a secondary rebuild overwrite the primary |
| 2026-11-14 | Admission is two-layer: a per-pod counter plus a Redis slot counter | Accepted | The local check rejects instantly; the shared counter makes the limit global, and Redis failure degrades to the old behaviour |
| 2026-11-14 | A concurrency slot has a lease TTL | Accepted | A pod killed mid-turn must not pin a tenant out of the service; the TTL is the recovery path |
| 2026-11-14 | The dollar budget rejects at admission (`budget_usd`) | Accepted | Rejecting before the turn avoids spending; recording after captures real usage |
| 2026-11-14 | Query logs are hashed by default; `redact`/`raw` are explicit | Accepted | A hash keeps queries correlatable without storing content; raw text in logs is a compliance risk |
| 2026-11-14 | Security headers are a pure-ASGI middleware; no CSP from the app | Accepted | `BaseHTTPMiddleware` interferes with SSE; the single-file viewer uses inline script, so the ingress owns any CSP |

## 10. Open questions

- Cloud provider and data-residency regions (AWS assumed).
- RPO/RTO targets and tenant tiers.
- Embedding dimension/model and quantization policy.
- Qdrant + OpenSearch vs OpenSearch-only.
- Trace analytics store (ClickHouse vs S3+Athena) — the archive of record is file/S3 gzipped JSONL.
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
| 2026-11-11 | Audit finding 1: SSE tokens emitted after the ground check, not from `generate` | ✅ | `9e9f643` |
| 2026-11-11 | Audit finding 2: per-request retrieval timings; shared instance state removed | ✅ | `9e9f643` |
| 2026-11-11 | Audit finding 3: budget reports the request that was sent; the input ceiling covers the prompt | ✅ | `9e9f643` |
| 2026-11-11 | Audit finding 4: idle conversation locks are pruned from the registry | ✅ | `b01f724` |
| 2026-11-11 | Audit finding 7: proxy-aware client IP for the rate limiter | ✅ | `b01f724` |
| 2026-11-11 | Audit finding 9: mypy clean and gated in CI | ✅ | `b01f724` |
| 2026-11-11 | Audit finding 5: async graph nodes, async providers, concurrent retrieval legs, bounded pool | ✅ | `03c72f4` |
| 2026-11-11 | Audit finding 8: ingest and eval answer `202 {job_id}` and run on a background worker | ✅ | `12514ce` |
| 2026-11-11 | Audit finding 6: `SparseStore` protocol, OpenSearch adapter, off-path index rebuild | ✅ | `0ba7279` |
| 2026-11-11 | Audit remediation documented across the tracker, audit and README | ✅ | `739a491` |
| 2026-11-11 | Stale local schema broke startup with no explanation; detected and recreated outside prod | ✅ | `266f809` |
| 2026-11-11 | OpenSearch index populated by ingestion; adapter run against a live cluster | ✅ | `dd48e7b` |
| 2026-11-11 | Sparse leg scores with Lucene's formula; both backends measure identically | ✅ | `beadfc5` |
| 2026-11-11 | Gate re-baselined to 0.9167; tie-break sensitivity measured and reported | ✅ | local |
| 2026-11-12 | Phase 2 tenant isolation: tenant_id stamped into chunk metadata (Chroma + OpenSearch), mandatory tenant filter in compilation, per-call tenant injection with forged-filter override, cross-tenant retrieval tests | ✅ | `411945d` |
| 2026-11-12 | Postgres RLS policies (migration 0002, conditional on Postgres) | ✅ | `411945d` |
| 2026-11-12 | Qdrant adapter scaffolding + optional `vector` extra | ◐ | `411945d` |
| 2026-11-12 | OpenSearch versioned indexes + alias cutover helpers | ◐ | `f99b685` |
| 2026-11-12 | Qdrant dense backend wired (config + factory + filter compilation) | ◐ | `1cb30a9` |
| 2026-11-12 | Phase 2 completion: shadow indexing/dual-read, tenant offboarding, retention API, JSONB migration, partition month, eval parity gate in CI, backend benchmark harness | ✅ | `6b57b3b` |
| 2026-11-12 | Phase 2 pushed to GitHub (31 commits); working tree clean | ✅ | `6b57b3b` |
| 2026-11-12 | Phase 3: `asearch` on DenseStore/SparseStore protocols and concrete stores/retrievers; HybridRetriever awaits both legs | ✅ | `28958c5` |
| 2026-11-12 | Phase 3: graceful shutdown/drain tests (lifespan stops job workers + executor) | ✅ | `84c55a2` |
| 2026-11-12 | Phase 3: SSE cancellation — disconnect-aware stream cancels the graph, releases locks, persists no partial turn | ✅ | `b454072` |
| 2026-11-12 | Phase 3: trace store split — file/S3 archive, archive-before-prune retention, paginated `/trace` | ✅ | local |
| 2026-11-12 | Phase 3: shared Redis job store + handler registry + `python -m blackbox.worker`; retention is a job | ✅ | local |
| 2026-11-12 | Phase 3: staged ingest with validation and alias swap (abort keeps the old version serving) | ✅ | local |
| 2026-11-12 | Phase 3 complete: 454 tests green, `ruff`/`mypy` clean; compose gains a worker service | ✅ | local |
| 2026-11-13 | Phase 4: Prometheus registry + `/metrics` + HTTP/SSE/retrieval/LLM/queue instrumentation | ✅ | `18c0266` |
| 2026-11-13 | Phase 4: Helm chart (API, worker, migration Job, HPA/KEDA, PDB, NetworkPolicy, Ingress, ServiceMonitor) | ✅ | `18c0266` |
| 2026-11-13 | Phase 4: unprivileged image; `alembic.ini`/migrations shipped for the migration Job | ✅ | `18c0266` |
| 2026-11-13 | Phase 4: recording rules + multi-window error-budget and SLO alerts | ✅ | `18c0266` |
| 2026-11-13 | Phase 4: CI coverage gate, chart lint/render + kubeconform, image build, Trivy scan, SPDX SBOM | ✅ | `18c0266` |
| 2026-11-13 | Phase 4: Terraform AWS reference (VPC/EKS/Aurora/ElastiCache/S3/KMS/Secrets Manager/IRSA) + External Secrets example | ✅ | `b5b8658` |
| 2026-11-13 | Phase 4: Terraform CI job (`fmt -check`, `init -backend=false`, `validate`, Trivy config) | ✅ | `b5b8658` |
| 2026-11-13 | Phase 4: GitOps delivery reference with Argo CD canary | ✅ | `8265389` |
| 2026-11-13 | Phase 4: load test harness and Chaos Mesh failure-injection experiments | ✅ | `00b44b7` |
| 2026-11-13 | Phase 4: global per-tenant LLM/token quotas (Redis-backed, fail open) + pen-test checklist + secret-rotation runbook | ✅ | `741ac75` |
| 2026-11-13 | Phase 4: synthetic probe and on-call runbook | ✅ | `24fd5aa` |
| 2026-11-13 | Phase 4: runtime warm-up, optional Sentry, Terraform backend shapes (OpenSearch/WAF/Route 53) and synthetic probe CronJob | ✅ | `11684bf` |
| 2026-11-13 | Phase 4: optional OpenTelemetry tracing — no-op facade, `otel` extra, spans for HTTP/chat/graph/retrieval/LLM/DB, trace id in logs | ✅ | `43683cc` |
| 2026-11-13 | Phase 4: chart OTEL env + prod collector endpoint/NetworkPolicy egress; image installs the `otel` extra | ✅ | `43683cc` |
| 2026-11-13 | Phase 4: Grafana overview dashboard + Prometheus/Loki/Tempo datasource provisioning | ✅ | `6a9af95` |
| 2026-11-13 | Phase 4: OTel Collector/Tempo/Loki configs + Promtail values (`trace_id` extraction); dashboard queries tested against the registry | ✅ | `6a9af95` |
| 2026-11-13 | Fix: worker process registers job handlers (imported the empty routes package) | ✅ | `18c0266` |
| 2026-11-14 | Phase 5: region config, `tenant_regions` (migration 0005), tenant-pinned writes (421), RLS GUC, read replica + pool policy | ✅ | local |
| 2026-11-14 | Phase 5: per-region retrieval topology/alias config, region-scoped names, rebuild-from-documents path + script | ✅ | local |
| 2026-11-14 | Phase 5: regional Redis `redrive()`, cache-fallback and queue-loss-risk metrics | ✅ | local |
| 2026-11-14 | Phase 5: per-node trace usage, cost derivation, conversation + tenant cost endpoints, `docs/cost-model.md` | ✅ | local |
| 2026-11-14 | Phase 5: failover/restore/tenant-move drills, `docker-compose.dr.yml`, runbooks, generated `docs/dr-drill-results.md` | ✅ | local |
| 2026-11-14 | Phase 5: AZ/store/provider/region chaos experiments + `scripts/chaos_verify.py` | ✅ | local |
| 2026-11-14 | Phase 5: gated Terraform multi-region layer (Aurora/Redis global, KMS/secret replicas, S3 CRR, Route 53 failover), GitOps ApplicationSet + region overlays | ✅ | local |
| 2026-11-14 | Hardening: global per-tenant concurrency (Redis slots + local fallback), dollar budget, PII/query-log policy, security headers | ✅ | local |
| 2026-11-14 | CI: `pip-audit` dependency audit and `gitleaks` secret scan; provider contract tests; opt-in Postgres/Redis integration tests | ✅ | local |
| 2026-11-14 | Docs: production quick start, PII/query-logging policy, tracker reconciliation | ✅ | local |

## 12. Update protocol

1. Move items between `⬜`, `◐`, `✅` and add a one-line note with the commit/PR.
2. Add a row to the change log; never rewrite past rows.
3. Add new work under the correct phase; add a new phase only for a genuinely new workstream.
4. Record architecture decisions in the decision log and mirror major ones in
   `docs/scaling-and-production-readiness.md`.
5. Keep the status dashboard consistent with the checklists; CI/test counts must be current.
