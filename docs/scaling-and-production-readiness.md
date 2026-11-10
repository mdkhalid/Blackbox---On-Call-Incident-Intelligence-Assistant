# Scaling & Production Readiness — Blackbox

Status: **Phase 0 complete; Phase 1 complete (control plane); Phase 2 next.** This document
is the production contract for the codebase: target architecture, capacity model, tenancy,
retrieval migration, runtime, operations and the phased path from the current single-node demo
to an org-wide, multi-region service. It is deliberately explicit about what is **implemented
now** versus **planned**.

Target deployment: Kubernetes (AWS EKS reference), org-wide scale (thousands of concurrent
conversations, 10M+ chunks, multi-region, strict tenant isolation).

---

## 1. Executive summary

The current application is a correct, measured, single-process corrective-RAG service:

- FastAPI + SSE, LangGraph, three-layer memory, hybrid retrieval, trace viewer and eval harness.
- Local Chroma for dense vectors; in-process BM25; Redis-shared rate limits and Postgres
  advisory turn locks; JWT/RBAC with tenant-scoped conversations.
- 333 tests, deterministic offline fallbacks, Postgres/SQLite conversation store.

It is suitable for a demo or a small internal team. It is **not** horizontally scalable as
written: the retrieval indexes are node-local, the graph is synchronous, long SSE turns still
run in a threadpool, tenant admission control is per pod, and data-plane tenant filters/RLS are
not yet in the stores.

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
| Fusion | App-side **RRF/weighted** | Store-native hybrid | Preserves measured 0.9214 MRR; stores scale independently |
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
  worker; dense and sparse legs run sequentially; filters compile to Chroma `where`.
- Retrieval cache key includes query/filter/top-k/config; invalidated on ingest.

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

### Gate

Hybrid MRR ≥ 0.9214 (or a documented new baseline), recall/precision not lower, groundedness
unchanged, tenant filters proven, p95 within SLO at target concurrency.

---

## 6. Runtime, graph and streaming

- Async provider interface; async graph nodes; concurrent dense/sparse calls; no threadpool
  hops in the request path.
- Explicit LLM policy: connect/read timeout, bounded retries with jitter, circuit breaker,
  per-tenant admission control, max tokens/cost. **Phase 1 implements timeout/retry, the
  circuit breaker, bounded provider concurrency and per-pod tenant admission; async provider
  execution and global quotas are Phase 3/4.**
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

---

## 8. Ingestion, evaluation and memory jobs

- `POST /api/ingest` and `/api/eval/run` return `202 {job_id}`; status/result endpoints.
- Workers consume SQS/Kafka; jobs are idempotent and checkpointed per document.
- Ingest: validate/scan → chunk → embed → staging index → validate → alias swap.
- Summary compression and memory compaction move to workers.
- One ingest per tenant at a time; eval queue isolated from ingest; dead-letter queues and
  retry budgets.
- Evaluation results persist for regression comparison and release gates.

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
| 2 | Qdrant/OpenSearch adapters, concurrent legs, mandatory tenant filters + RLS, versioned indexes, shadow/dual-read cutover | ⬜ Planned |
| 3 | Async graph/providers, queue workers, trace store split | ⬜ Planned |
| 4 | Helm/Terraform, HPA/KEDA, OTel/Prometheus/Grafana, CI/CD, canary, security hardening | ⬜ Planned |
| 5 | Multi-region, tenant-pinned writes, DR drills, cost controls, chaos/load suite | ⬜ Planned |

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
- [ ] HPA/KEDA and PDBs; load test at target concurrency.
- [ ] Dashboards/alerts/SLO burn; on-call runbooks.
- [ ] Multi-region failover and RTO/RPO drill.
- [ ] Image scan/SBOM; dependency and secret scanning.

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
