# Scaling & Production Readiness — Blackbox

Status: **Phases 0–3 complete (control plane, retrieval/tenant isolation, async runtime and
jobs); Phase 4 (Kubernetes and operations) in progress — Helm, the Terraform AWS reference
(+ optional OpenSearch/WAF/Route 53), image hardening, Prometheus metrics/SLO alerts,
optional OpenTelemetry tracing, Grafana/Loki/Tempo configs, GitOps/canary reference,
load/chaos assets, global per-tenant quotas, pen-test checklist, secret-rotation runbook,
synthetic probe, on-call runbook, runtime warm-up, optional Sentry, External Secrets and CI
image/security gates landed; remaining Phase 4 items are global per-tenant concurrency,
dependency/secret scanning in CI, applying Terraform to a real account, the live-cluster
backend benchmark and running the load/chaos suites.**
This document is the production contract for the codebase: target architecture, capacity
model, tenancy, retrieval migration, runtime, operations and the phased path from the current
single-node demo to an org-wide, multi-region service. It is deliberately explicit about what
is **implemented now** versus **planned**.

Target deployment: Kubernetes (AWS EKS reference), org-wide scale (thousands of concurrent
conversations, 10M+ chunks, multi-region, strict tenant isolation).

---

## 1. Executive summary

The current application is a correct, measured, single-process corrective-RAG service:

- FastAPI + SSE, LangGraph, three-layer memory, hybrid retrieval, trace viewer and eval harness.
- Local Chroma for dense vectors; in-process BM25; Redis-shared rate limits and Postgres
  advisory turn locks; JWT/RBAC with tenant-scoped conversations.
- 465 tests, deterministic offline fallbacks, Postgres/SQLite conversation store.

It is suitable for a demo or a small internal team. It is **not** yet horizontally scalable
as written: the dense retrieval index is node-local (Qdrant and OpenSearch adapters are
opt-in), and tenant admission control is per pod. The data-plane tenant filters, RLS and
the tenant-isolated retrieval predicates are implemented, and the job queue can be shared
through Redis with a dedicated worker process; the remaining gaps are the opt-in backend
cutover, global quotas, and the Phase 4 deployment/observability work.

The target separates state from compute:

```
Global DNS (latency + tenant home region)
  → WAF / Ingress (TLS, body limits, SSE timeouts)
    → stateless API pods (async graph, auth/tenant context)
      → Redis (limits, idempotency, cache)
      → Postgres (conversations, tenants, leases, entities)
      → Qdrant (dense) + OpenSearch (sparse)
      → queue → workers (ingest, eval, compaction)
      → LLM gateway (quotas, retries, streaming, cost)
      → OpenTelemetry → Prometheus/Grafana/Loki/Tempo; traces → ClickHouse/S3
```

**Invariant:** any API pod can serve any request; no authoritative state lives in a pod or on a
local disk. Tenant writes are pinned to the tenant's home region.

---

## 2. Scale assumptions and SLOs

### Design targets (provisional; confirm before Phase 5)

| Dimension | Target |
|---|---|
| Concurrent SSE conversations | 2,000 globally |
| Chat turns | 200/s peak |
| Corpus | 10M+ chunks, tenant-scoped |
| Retrieval p95 | ≤ 400 ms at target load |
| Time to first graph node | p95 ≤ 300 ms |
| Complete answer | p95 ≤ 8 s |
| API availability | 99.9% monthly |
| RPO / RTO | ≤ 5 min / ≤ 1 h |
| Cross-tenant reads | **zero** — hard security gate |

These are load-test targets, not current measurements. The first action in Phase 2 is a
capacity test that replaces these estimates with measured numbers.

### Capacity implications

- **Vectors:** 10M chunks × 1536 float32 dims ≈ 61 GB raw, plus HNSW overhead (roughly
  1.5×) and replicas. 384-dim embeddings reduce raw storage to ≈ 15 GB. Decide embedding
  dimension and quantization before sizing Qdrant. Shard by tenant hash; replicate per AZ.
- **Sparse:** OpenSearch index size is driven by text + inverted index (typically 1–2× the
  chunk text). Shard by tenant/time; force-merge old indices.
- **LLM:** 200 turns/s × ~2.2 calls/turn ≈ 440 provider calls/s at peak. The LLM gateway,
  not the API pods, is the real concurrency ceiling. Quotas, batching and caching are required.
- **SSE:** thousands of idle streams are cheap in async Python; the limit is per-turn work
  (LLM latency, retrieval, DB leases). Never hold a pooled DB connection for a stream.
- **Postgres:** ~200 turn writes/s plus memory JSONB updates; partition turns by tenant hash
  and month; PgBouncer in transaction mode; separate connections for leases.

---

## 3. Technology decisions

| Concern | Decision | Alternatives considered | Why |
|---|---|---|---|
| Dense store | **Qdrant** cluster | pgvector, Milvus, Weaviate | Payload filters, snapshots, shard/replica model, predictable 10M+ behavior |
| Sparse store | **OpenSearch** | Postgres FTS, Elasticsearch | Mature BM25, analyzers, independent scaling, cross-cluster replication |
| Fusion | App-side **RRF/weighted** | Store-native hybrid | Preserves measured 0.9167 MRR; stores scale independently |
| Relational | **PostgreSQL + PgBouncer** | Aurora Serverless, Cloud SQL | JSONB, partitions, RLS, mature ops |
| Cache/limits | **Redis cluster** | Memcached, in-process | Shared token buckets, idempotency, cache, short TTLs |
| Turn lock | **Postgres lease/advisory lock, dedicated NullPool engine** | Redis Redlock | Correctness; no pooled connection held during SSE |
| LLM | **Gateway + async clients** | Direct SDK calls | Quotas, retries, circuit breakers, model tiering, cost |
| Jobs | **SQS** (Kafka above ~10k msg/s) | Celery/Redis, Temporal | Durable, decoupled workers, simple retry/DLQ |
| Traces | Postgres recent + **ClickHouse/S3** | Postgres only | Bounded OLTP growth; analytics and retention |
| Deploy | Helm + Terraform + GitOps | Compose, serverless framework | Multi-region, canary, repeatable |

**Open trade-off:** Qdrant + OpenSearch is two operational stacks. The alternative is
OpenSearch kNN for both dense and sparse, which reduces ops but couples scaling and may lose
dense quality. Decide after the Phase 2 parity benchmark.

---

## 4. Tenancy, authentication and data isolation

### Model

- OIDC/JWT with `tenant_id`, `roles`, optional `region` and `tier` claims.
- Roles: `viewer` (read conversations), `operator` (chat/read), `admin` (ingest/eval/tenant
  administration). Service-to-service uses mTLS or scoped API keys.
- Every row and vector payload carries `tenant_id`. Every repository method and retrieval
  query requires tenant context; missing context fails closed (400/403), never “all tenants”.
- Postgres RLS as defense in depth, in addition to application filters.
- Default shared Qdrant collection/OpenSearch index with mandatory tenant filters; dedicated
  collection/index for regulated or large tenants.
- Data residency: tenant home region owns writes. Cross-region reads route through the home
  region; no cross-region conversation writes.
- Audit events for conversation access, ingest, eval, model/prompt version, tokens and cost.

### Authorization matrix (required tests)

| Action | viewer | operator | admin | cross-tenant |
|---|---|---|---|---|
| Read conversation/trace/memory/budget | ✓ | ✓ | ✓ | 404 |
| Chat | — | ✓ | ✓ | 404 |
| Ingest / eval / tenant admin | — | — | ✓ | 403 |

### Status

**Implemented at the application layer (Phase 1):** JWT validation (HS256 dev, JWKS
RS256/ES256), role enforcement (`viewer`/`operator`/`admin`), tenant claims required when
auth is enabled, tenant-scoped conversation reads/writes, admin-only ingest/eval, and
cross-tenant tests (read/chat → 404, admin actions → 403).

**Still required:** mandatory tenant filters in the external retrieval stores and Postgres RLS
(both Phase 2, where the stores are replaced), plus tenant offboarding.

---

## 5. Retrieval architecture and index lifecycle

### Current

- Chroma `PersistentClient` on a local volume; BM25 built in-process from the full corpus per
  worker; dense and sparse legs run concurrently; filters compile to Chroma `where`.
- Retrieval cache key includes query/filter/top-k/config; invalidated on ingest.
- The sparse leg sits behind the `SparseStore` protocol (`blackbox/retrieval/stores.py`), with
  the in-process BM25 index as the development adapter and an OpenSearch adapter alongside it.
  The metadata filter is a store-neutral AST compiled per store.
- The corpus-derived indexes are rebuilt on the ingestion worker and published atomically
  (`rebuild_indexes`), so a request never pays for an O(corpus) rebuild.

### Target

- `DenseStore` / `SparseStore` protocols; Qdrant and OpenSearch adapters.
- Dense and sparse queries run concurrently; fusion and rerank remain application-side.
- Neutral metadata-filter AST compiled to Qdrant payload filters and OpenSearch bool queries.
- Mandatory tenant predicate injected after compilation; adapters reject an empty tenant.
- Versioned physical indexes (`chunks_vN`) behind stable aliases (`chunks-current`).
  Ingest writes staging, validates counts/spot queries, then atomically switches the alias.
  Rollback is an alias switch; dual-read during cutover.
- Embedding model/dimension are part of index identity; model changes create a new index.
- Per-tenant concurrency limits on retrieval and reindex jobs.

### Migration

1. Adapters + contract tests against Qdrant/OpenSearch containers.
2. Shadow-index the current corpus; compare top-k and eval metrics.
3. Dual-read shadow traffic; require MRR/R@k parity.
4. Cut reads; keep Chroma read-only for rollback.
5. Remove local Chroma/BM25 from the runtime and docs.

> **State (2026-11-11, audit finding 6).** Steps 1's *seam* is done: the protocols, the
> per-store filter compilation and the OpenSearch adapter exist and are contract-tested
> against the in-process adapter. The *migration* is not: the OpenSearch index is not
> populated by ingestion and the adapter has not been run against a live cluster. Chroma
> plus the in-process BM25 remain the default and the rollback path. Steps 2–5 are open, as
> are the tenant predicate and the Qdrant adapter.

### Gate

Hybrid MRR ≥ 0.9167 (or a documented new baseline), recall/precision not lower, groundedness
unchanged, tenant filters proven, p95 within SLO at target concurrency.

> **The baseline was re-measured on 2026-11-11** and is now the one the gate uses: dense 0.8548,
> sparse 0.8714, hybrid 0.9167, hybrid+rerank 0.8857. The previous figures were taken with
> `rank_bm25`, whose inverse document frequency differs from Lucene's — and Lucene is what
> the OpenSearch backend scores with. Re-scoring with the formula the production backend
> actually uses moved the sparse leg up by 0.0143 and the hybrid down by 0.0047, a difference
> six times smaller than the metric's own sensitivity to tie-breaking (0.0286, measured by
> `--tie-break-sweep`). The last decimal place of the hybrid MRR is not a finding.

---

## 6. Runtime, graph and streaming

- Async provider interface; async graph nodes; concurrent dense/sparse calls; no threadpool
  hops in the request path.
- Explicit LLM policy: connect/read timeout, bounded retries with jitter, circuit breaker,
  per-tenant admission control, max tokens/cost. **Phase 1 implements timeout/retry, the
  circuit breaker, bounded provider concurrency and per-tenant admission; global quotas are
  Phase 4.**
- SSE: stateless streams, configurable ingress idle timeout, no proxy buffering; tokens remain
  post-ground-check; node events stream during execution.
- Idempotency: `Idempotency-Key` on `/api/chat`, stored in Redis with the final result, so
  client retries do not duplicate turns. A Redis `SET NX` lock rejects concurrent duplicates
  with `409` while the first request is in flight.
- Backpressure: max concurrent turns per tenant/pod; 429/503 with `Retry-After` when the LLM
  or database is saturated.
- Optional LangGraph Postgres checkpointer for crash recovery; Postgres remains the
  conversation source of truth.
- Graceful shutdown: stop accepting, drain SSE, release leases, flush traces.

> **State (2026-11-11, audit finding 5).** The async provider interface, the async graph
> nodes, the concurrent dense/sparse legs and the explicitly bounded worker pool
> (`MAX_WORKER_THREADS`, replacing asyncio's `min(32, cpu_count + 4)`) are implemented. A
> hosted LLM round trip no longer occupies a worker thread.
>
> Two things are still open. The *retrieval stores* are synchronous and reached through
> `asyncio.to_thread`, so the request path still has threadpool hops for Chroma and BM25.
> And the provider's concurrency limit is per-process; global quotas remain Phase 4.

---

## 7. Persistence, memory and traces

- SQLAlchemy 2.0 `Mapped` models + **Alembic** migrations (replace ad-hoc DDL).
- Tables: tenants, conversations, turns, entity facts, memory summaries, leases, trace
  pointers. Tenant/region columns and indexes on `(tenant_id, updated_at)` and
  `(tenant_id, conversation_id, id)`.
- JSONB for memory and trace payloads. Partition turns/traces by tenant hash + month.
- Full traces go to ClickHouse/S3; Postgres keeps the last turn and a pointer. `/trace` is
  paginated.
- Retention and deletion APIs; tenant offboarding removes rows, vectors, search documents,
  cache keys and archives.
- PgBouncer transaction mode; separate `NullPool` engine for advisory locks; pool sizing
  documented per replica.

> **State (2026-11-11, audit finding 9).** The models use `Mapped[]`/`mapped_column()` and
> the DDL is unchanged, so the Alembic baseline still matches. Two further database fixes
> landed with the audit remediation: an in-memory SQLite database now uses a static pool (a
> `:memory:` database belongs to a connection, and the API's thread-pooled database calls
> were seeing a different, empty database per thread), and a local SQLite database whose
> schema no longer matches the models is detected and recreated outside production.
> `create_all` never alters an existing table, so without the second fix a local database
> written before a schema change made the app unstartable.

---

## 8. Ingestion, evaluation and memory jobs

- `POST /api/ingest` and `/api/eval/run` return `202 {job_id}`; status/result endpoints.
- Workers consume SQS/Kafka; jobs are idempotent and checkpointed per document.
- Ingest: validate/scan → chunk → embed → staging index → validate → alias swap.
- Summary compression and memory compaction move to workers.
- One ingest per tenant at a time; eval queue isolated from ingest; dead-letter queues and
  retry budgets.
- Evaluation results persist for regression comparison and release gates.

> **State (2026-11-12, Phase 3).** The `202 {job_id}` shape and the status endpoints
> are implemented (`/api/jobs/{id}`, `/api/ingest/{id}`, `/api/eval/{id}`), along with a real
> queue: a second ingest or eval is queued rather than refused with `409`. Ingest, eval and
> retention are described by `kind + params`, so with `REDIS_URL` set the job record and the
> pending queue live in Redis and any replica — or the dedicated `python -m blackbox.worker`
> process — executes them. A job submitted on one pod is observable from every pod.
>
> Closure-based jobs remain local-only: a callable cannot travel through Redis, so if another
> pod wins the dequeue the job fails with a clear reason instead of running the wrong code.
> Dead-letter queues and retry budgets remain open. Staged ingest now builds a complete new
> index version, validates documents/chunks/tenant coverage, and swaps the alias only on
> success, so a failed rebuild never serves partial data. Summary compaction stays inline in
> the turn: it mutates the memory the same turn reads, so queueing it would race the turn.
> The same Phase 3 work added SSE cancellation (a disconnect cancels the graph and releases
> the turn lock) and the trace store split (Postgres recent + file/S3 archive, paginated
> `/trace`, archive-before-prune retention).

---

## 9. Kubernetes topology

| Workload | Scaling |
|---|---|
| API | Deployment, 3+ replicas/AZ, HPA on CPU + active SSE + p95 |
| worker-ingest | KEDA on queue depth |
| worker-eval | KEDA on queue depth, isolated concurrency |
| worker-memory | KEDA on compaction queue |

- PDBs, topology spread across AZs, anti-affinity, resource requests/limits.
- Startup probe after runtime warm-up; readiness calls the real `/health/ready`; liveness is
  process-only.
- Ingress: TLS, body-size limit, SSE proxy buffering off, idle timeout greater than the longest
  turn.
- NetworkPolicies: API → stores/Redis/queue/gateway only; workers → stores/queue only.
- No local persistent volumes for authoritative data.

### Reference infrastructure (Terraform, AWS)

EKS, Aurora PostgreSQL, ElastiCache Redis, SQS/MSK, OpenSearch, Qdrant on EKS (or managed),
S3, KMS, WAF, Route 53, Secrets Manager, CloudWatch. Two regions; per-region stores; global DNS
with tenant affinity.

> **State (2026-11-13, Phase 4).** The Kubernetes half of this section is implemented as a
> chart in [`deploy/helm/blackbox`](../deploy/helm/blackbox): API and worker Deployments,
> HPA and KEDA, PodDisruptionBudgets, topology spread, SSE-correct Ingress annotations,
> NetworkPolicy templates, a ServiceMonitor, and an `alembic upgrade head` Job that runs as a
> pre-install/pre-upgrade hook. The image runs as uid/gid 10001.
>
> The AWS half is written as a self-contained Terraform root module in
> [`deploy/terraform`](../deploy/terraform): VPC with flow logs, EKS (private endpoint by
> default, CMK-encrypted Secrets, tainted batch node group, managed add-ons), Aurora
> PostgreSQL 16 with an RDS-managed master password, ElastiCache Redis with TLS and
> `noeviction`, a versioned KMS-encrypted S3 trace archive, and split IRSA roles for the app
> and External Secrets. It is `terraform validate`- and Trivy config-clean, but has **not been
> applied** (no AWS account here), and it deliberately excludes OpenSearch/Qdrant until the
> Phase 2 benchmark decides the shape. [`deploy/external-secrets`](../deploy/external-secrets)
> assembles the chart's `existingSecret` from those secrets.
>
> Runtime warm-up is still open: readiness calls `/health/ready` (which checks the
> conversation store) but does not build the retriever or LLM, so the first chat after a pod
> starts pays cold start. The chart's startup probe allows up to 60s, which is enough to warm
> on startup once that is wired.

---

## 10. Security

- TLS everywhere; WAF; body limits at edge and app; CORS allowlist; security headers.
- Secrets in Secrets Manager/External Secrets; no keys in images/logs; rotation documented.
- JWT validation (issuer, audience, expiry, signature), role checks, tenant binding.
- Prompt-injection defense: retrieved content treated as data, flags logged, strict mode
  available; do not rely on heuristics alone at the edge.
- PII redaction in logs; query text logging configurable or hashed in regulated deployments.
- Audit trail for access and admin actions; retention policy.
- Dependency/image scanning, SBOM, signed images, least-privilege service accounts.
- RLS + mandatory application tenant filters; cross-tenant access tests are release blockers.
- The rate limiter keys on the *client* address, not the TCP peer. Behind the ingress every
  request arrives from the proxy, so the peer address would make a per-client limit a
  cluster-wide one. `X-Forwarded-For` is read only when the immediate peer is a configured
  trusted proxy (`TRUSTED_PROXY_CIDRS`); otherwise the header is client-controlled and is
  ignored, so the limit cannot be bypassed with a spoofed header.

---

## 11. Observability and SLOs

- OpenTelemetry traces: HTTP → graph node → retrieval store → LLM → DB. Propagate request and
  tenant ids.
- Metrics: request rate/latency/errors, SSE active connections, queue depth/lag, retrieval
  stage latency, LLM latency/tokens/cost per node and tenant, cache hit rate, DB pool
  saturation, lease contention.
- Logs: structured JSON, tenant/request ids, no secrets; PII redaction.
- Dashboards and alerts: SLO burn, 5xx, provider 429/5xx, queue lag, quota exhaustion,
  index lag, replica lag.
- Error tracking with release/version tags.
- Synthetic probes per region; read-only smoke through retrieval and SSE.

> **State (2026-11-13, Phase 4).** The metrics half is implemented. `GET /metrics` serves a
> dependency-free Prometheus exposition with bounded labels: HTTP requests/duration by route
> template, `blackbox_sse_active_streams`, retrieval stage histograms (dense, sparse, fuse,
> rerank, cache, total), LLM calls/duration/tokens/errors by graph node, and job queue depth
> by kind. The job queue gauge is read at scrape time so it reflects work drained by another
> replica. `deploy/observability/prometheus-rules.yaml` adds recording rules and alerts for
> the documented SLOs: absent-service, 14.4×/6× multi-window error-budget burn, answer p95,
> retrieval p95, LLM error ratio and queue backlog. The HTTP path label is the route template,
> never a conversation id; tenant-level cost must aggregate from the existing counters rather
> than add an unbounded label.
>
> Still open: synthetic probes and error tracking. The metric names are the contract the
> dashboards build against.

> **Dashboards and stack configs (2026-11-13, Phase 4).** `deploy/observability` ships a
> Grafana overview dashboard pinned to those metric names, Prometheus/Loki/Tempo datasource
> provisioning, an OTel Collector pipeline (memory limiter, batching, cluster label, Tempo
> exporter), single-binary Tempo and Loki references, and Promtail values that keep only the
> Blackbox namespace and extract `trace_id` so log lines link into traces. The stack files
> are offline references for development; production uses the upstream charts with object
> storage. Tests in `tests/test_observability_assets.py` fail the dashboard when a panel
> queries a metric the app no longer exports.

> **Tracing (2026-11-13, Phase 4).** OpenTelemetry tracing is implemented as an optional
> capability: `blackbox/observability/tracing.py` exposes a narrow facade (span, attributes,
> exception, trace id) that is a no-op unless `OTEL_ENABLED=true` and the `otel` extra is
> installed. Spans cover `http.request` (pure ASGI, so it stays open for the whole SSE body),
> `chat.turn`, `db.record_turn`, `retrieval.retrieve`, `llm.complete` and `graph.node.*`.
> Attributes use route templates and bounded values; `trace_id` is bound into the structlog
> context so a JSON log line can be joined to its trace. OTLP/HTTP exports to a collector,
> which is the deployment seam for sampling and backend choice. Span names are stable and
> documented in `deploy/observability/README.md`; the worker does not call
> `configure_tracing` yet, so job tracing remains open.

---

## 12. Multi-region and DR

- Active-active API/retrieval in two regions; **tenant home region owns conversation writes**.
- Postgres regional primary with cross-region replica/Aurora Global for DR; promote on
  failover. Conversations remain pinned to the surviving home region.
- Qdrant/OpenSearch per region with async replication; rebuild from canonical documents is
  the fallback.
- Redis is regional and non-authoritative; safe to lose.
- Failover runbook: DNS shift, queue drain, lease recovery, index health, tenant notification,
  verification queries.
- Backups and restore drills; RPO/RTO measured, not assumed.

---

## 13. Cost model

Cost drivers, in order: LLM tokens, vector RAM/replicas, OpenSearch, Postgres, network egress,
observability retention.

Controls:
- Per-tenant token/cost quotas and budgets; model tiering (cheap grade/rewrite, stronger
  generation); prompt/response caching where safe; embedding cache; batch embeddings.
- Quantized vectors where quality allows; shard/replica sizing by tenant tier.
- Trace retention tiers (hot Postgres, warm ClickHouse, cold S3).
- Per-conversation and per-tenant cost reporting from existing usage counters.

A monthly cost estimate requires provider pricing and target traffic; calculate from measured
tokens/conversation and storage per 1M chunks before Phase 2.

---

## 14. Phased roadmap and current status

| Phase | Scope | Status |
|---|---|---|
| 0 | This document, architecture decisions, SLOs, tenancy/runbook outline | ✅ Done |
| 1 | Auth/tenant scoping, Alembic, Redis limits/idempotency, dedicated lock pool, LLM timeout/retry/circuit breaker, body limits, readiness, multi-pod smoke | ✅ Done (control plane) |
| 2 | Qdrant/OpenSearch adapters, concurrent legs, mandatory tenant filters + RLS, versioned indexes, shadow/dual-read cutover | ✅ Done (live-cluster benchmark pending) |
| 3 | Async graph/providers, queue workers, trace store split | ✅ Done |
| 4 | Helm/Terraform, HPA/KEDA, OTel/Prometheus/Grafana, CI/CD, canary, security hardening | ◐ Helm + Terraform AWS reference + Prometheus/SLO alerts + OpenTelemetry traces + Grafana/Loki/Tempo configs + GitOps/canary reference + load/chaos assets + global per-tenant quotas + pen-test/rotation runbooks + External Secrets + CI image/scan/SBOM done; Terraform backend shapes open |
| 5 | Multi-region, tenant-pinned writes, DR drills, cost controls, chaos/load suite | ⬜ Planned |

### Audit remediation (complete, 2026-11-11)

The code audit (see [`code-audit-findings.md`](code-audit-findings.md)) found one critical
defect and eight scaling and quality gaps. All nine are fixed; the per-finding analysis and
resolution live in that document. What changed against this document's target shape:

- **Runtime (§6).** Async providers, async graph nodes, concurrent dense/sparse legs and an
  explicitly bounded worker pool. A hosted LLM round trip no longer holds a worker thread.
- **Retrieval (§5).** The sparse leg is behind the `SparseStore` protocol, with the OpenSearch
  adapter written and contract-tested. The index is not yet populated and the adapter has not
  met a live cluster, so Chroma + in-process BM25 remain the default and the rollback path.
- **Jobs (§8).** `202 {job_id}` with status endpoints and a real queue, so a second job is
  queued rather than refused. With `REDIS_URL` set the job record and pending queue live in
  Redis, so any replica or the dedicated `python -m blackbox.worker` process executes them;
  without it the queue is in-process, which is the single-pod fallback rather than the
  target SQS/Kafka design.
- **Observability.** Retrieval timings are per-request rather than shared between concurrent
  requests, and the budget endpoint reports the request that was actually sent, including
  the exact prompt token count.
- **Security (§10).** The rate limiter keys on the client address, honouring
  `X-Forwarded-For` only from trusted proxies.
- **Quality.** `mypy` is clean and gated in CI.

### Phase 1 progress (complete, control plane)

Implemented:
- JWT auth + RBAC (`viewer`/`operator`/`admin`), tenant-scoped conversation repository and
  cross-tenant authorization tests. Auth is disabled only in dev; prod requires it.
- Alembic baseline migration covering conversations, turns and entity tables, with an
  upgrade/downgrade test and a CI migration check. Production runs `alembic upgrade head`.
- Redis-backed fixed-window rate limiting with local fallback; `Idempotency-Key` result replay
  and in-flight dedupe (Redis `SET NX`, `409` while in flight).
- Per-pod per-tenant turn admission control with `429` + `Retry-After`.
- Dedicated `NullPool` engine for Postgres advisory turn locks (no pooled request connection
  held for the SSE duration).
- `ResilientLLMProvider`: jittered retries, circuit breaker, bounded concurrency.
- Request-body size cap before JSON parsing; `/health/ready` checks the conversation store.
- Audit log lines for chat, ingest and eval.
- Two-replica Docker smoke green: shared Postgres conversation read and shared Redis limit.

Data-plane isolation (retrieval tenant predicates and Postgres RLS) is a Phase 2 gate because
the retrieval stores are replaced there. Tenant offboarding and global quotas are also Phase 2.

---

## 15. Production readiness checklist

### Security
- [ ] Auth enabled; roles enforced; tenant filters fail closed.
- [ ] RLS verified; cross-tenant matrix green.
- [ ] Secrets manager, TLS, WAF, CORS, body limits, security headers.
- [ ] Audit log and PII redaction verified.

### Data
- [ ] Alembic migrations applied in CI/CD; rollback tested.
- [ ] Backups and restore drill; retention/deletion tested.
- [ ] Tenant offboarding removes all copies.

### Retrieval
- [ ] Qdrant/OpenSearch contract tests; index alias cutover tested.
- [ ] Eval parity gate passed; tenant-filter proof.
- [ ] Reindex rollback rehearsed.

### Runtime
- [ ] Async path free of threadpool/blocking calls.
- [ ] LLM timeout/retry/circuit breaker and quotas verified under 429/5xx.
- [ ] SSE drain and graceful shutdown tested; idempotency tested.

### Operations
- [x] HPA/KEDA and PDBs (chart: HPA on CPU with custom-metric override, KEDA on Redis queue depth)
- [ ] Load test at target concurrency; failure injection
- [x] Recording rules and SLO/error-budget alerts (`deploy/observability`)
- [x] OpenTelemetry traces to an OTLP collector (optional `otel` extra; no-op when disabled)
- [x] Grafana overview dashboard + Prometheus/Loki/Tempo provisioning
- [x] Secret rotation runbook and pen-test checklist (`docs/`)
- [x] Synthetic probe (`scripts/smoke_probe.py`) and on-call runbook (`docs/on-call-runbook.md`)
- [ ] Multi-region failover and RTO/RPO drill
- [ ] Multi-region failover and RTO/RPO drill
- [x] Image scan/SBOM (CI: Trivy + SPDX)
- [ ] Dependency and secret scanning in CI

---

## 16. Open decisions

1. Cloud provider and regions (AWS assumed); data-residency constraints.
2. RPO/RTO and tenant tiers (free/enterprise) for dedicated indexes.
3. Embedding dimension/model and quantization policy.
4. Qdrant + OpenSearch vs OpenSearch-only (decide after Phase 2 benchmark).
5. Queue choice at projected event rate (SQS now, Kafka threshold defined).
6. Whether traces live in ClickHouse or S3+Athena for the first release.
7. Tenant migration between regions (data move procedure and maintenance window).

## 17. Runbook index (to be authored in Phase 4)

- Provider outage / 429 storm.
- Redis unavailable (degrade to DB + local limits).
- Postgres failover and lease recovery.
- Qdrant/OpenSearch node loss and index rebuild.
- Bad ingest / partial corpus / alias rollback.
- Tenant isolation incident.
- Region failover and failback.
