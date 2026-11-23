# Running Blackbox in production

One place to go from "fresh host" to "answering incident questions". For the architecture,
eval results and design decisions, see [`README.md`](../README.md) and the other files in
this directory.

> **What this service is:** a corrective-RAG assistant. It is a FastAPI service with a
> deterministic LangGraph pipeline (router → retrieve → grade → bounded rewrite → generate →
> ground check) and a background worker. It is **not an autonomous agent**: it does not
> choose tools or act on its own; the LLM is a narrow `complete()` seam, and every factual
> claim is verified against retrieved evidence before it is shown.

---

## 1. What production needs

| Requirement | Why | Notes |
|---|---|---|
| **PostgreSQL 16** | Conversations, turns, traces, tenant-region map, RLS | Managed service recommended |
| **Redis 7** | Shared rate limit, idempotency, quotas, concurrency slots, job queue | `maxmemory-policy=noeviction` |
| **LLM provider key** | Generation, grading, rewrite | `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`; prod refuses to start without it |
| **Auth material** | Bearer tokens with a tenant | `AUTH_JWT_SECRET` (HS256) **or** `AUTH_JWKS_URL` + issuer/audience (OIDC) |
| **TLS edge / reverse proxy** | Public entry, SSE must not be buffered | Compose ships Caddy; Helm ships an SSE-correct Ingress |
| **A domain or address** | Caddy automatic HTTPS | Optional; `:80` works on a private network |

Optional, for scale or operations:

| Requirement | When |
|---|---|
| **Qdrant** (dense) + **OpenSearch** (sparse) | More than one API replica; Chroma/BM25 are node-local |
| **S3-compatible bucket** | Durable trace archive (`TRACE_ARCHIVE_BACKEND=s3`) |
| **OTel collector + Prometheus** | Traces and the SLO alert rules in `deploy/observability` |
| **Sentry DSN** | Error tracking (`sentry` extra) |

The app is deliberately fail-fast in prod: missing auth, a missing provider key,
a wildcard CORS origin, `RLS_ENABLED` without Postgres, or `s3` archiving without a bucket
is refused at startup rather than discovered at the first request.

---

## 2. Minimal production environment

Create `deploy/compose/.env.prod` from the example and fill these in:

```dotenv
ENV=prod
BLACKBOX_DOMAIN=blackbox.example.com     # or :80 without TLS
CORS_ORIGINS=                            # comma-separated if a browser calls the API

AUTH_JWT_SECRET=$(openssl rand -hex 32)  # or AUTH_JWKS_URL + issuer/audience
OPENAI_API_KEY=sk-...
GENERATION_MODEL=gpt-4o
GRADING_MODEL=gpt-4o-mini
REWRITE_MODEL=gpt-4o-mini

DATABASE_URL=postgresql://user:pass@db.internal:5432/blackbox
REDIS_URL=rediss://:pass@redis.internal:6379/0

TRUSTED_PROXY_CIDRS=172.16.0.0/12        # the edge's network; X-Forwarded-For is only
                                         # honoured from these peers
TENANT_QUOTA_ENABLED=true
API_REPLICAS=2
WORKER_REPLICAS=1
```

For the bundled local Postgres/Redis (`docker-compose.prod-local.yml`), use the compose
service names instead:

```dotenv
DATABASE_URL=postgresql://blackbox:<POSTGRES_PASSWORD>@postgres:5432/blackbox
REDIS_URL=redis://redis:6379/0
POSTGRES_PASSWORD=<a strong password>
```

Keep `RLS_ENABLED=false` until the database role is provisioned correctly (see §8).

---

## 3. Start it

### Path A — Docker Compose (any cloud, VM, or laptop)

Managed Postgres/Redis:

```bash
cp deploy/compose/.env.prod.example deploy/compose/.env.prod
# edit .env.prod
docker compose -f deploy/compose/docker-compose.prod.yml \
  --env-file deploy/compose/.env.prod up -d
```

All-in-one server with local Postgres/Redis:

```bash
docker compose -f deploy/compose/docker-compose.prod.yml \
  -f deploy/compose/docker-compose.prod-local.yml \
  --env-file deploy/compose/.env.prod up -d
```

Scale API and worker independently:

```bash
docker compose -f deploy/compose/docker-compose.prod.yml \
  --env-file deploy/compose/.env.prod up -d --scale api=4 --scale worker=2
```

What happens: `migrate` runs `alembic upgrade head` once; API and worker start only after
it succeeds. The API has **no published port** — Caddy is the only public entry point and
load balances across replicas.

### Path B — Kubernetes (Helm)

```bash
helm upgrade --install blackbox deploy/helm/blackbox \
  --namespace blackbox --create-namespace \
  -f deploy/helm/blackbox/values-prod.example.yaml
```

The chart runs the migration as a `pre-install/pre-upgrade` hook, reads secrets from
`existingSecret` (see `deploy/external-secrets`) and wires HPA, KEDA, PDBs, NetworkPolicy
and an SSE-correct Ingress. The optional AWS infrastructure is in `deploy/terraform`; it is
not required.

### Bare process (no container)

```bash
pip install -e ".[rag,db,providers,search,vector,archive]"
export ENV=prod AUTH_ENABLED=true AUTH_JWT_SECRET=... OPENAI_API_KEY=...
export DATABASE_URL=... REDIS_URL=...
alembic upgrade head
uvicorn blackbox.main:app --host 0.0.0.0 --port 8000
# separate process for jobs:
python -m blackbox.worker
```

Run the API as one process per container (or behind a process manager) and scale by adding
replicas. Do not rely on `--workers` for tenant isolation; the shared state is in Redis and
Postgres.

---

## 4. First-run: seed the corpus

Ingestion requires an `admin` token and is a background job:

```bash
TOKEN=$(... a JWT with tenant_id and roles=["admin"] ...)

curl -X POST https://blackbox.example.com/api/ingest \
  -H "authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"corpus_dir":"seed/corpus"}'
# -> 202 {"job_id":"...","kind":"ingest","status":"queued"}

curl -H "authorization: Bearer $TOKEN" \
  https://blackbox.example.com/api/ingest/<job_id>
# -> {"status":"succeeded","result":{"documents":60,"chunks":315,...}}
```

Ingestion is idempotent and tenant-scoped, so a retry does not duplicate chunks. In a
multi-tenant deployment, ingest with each tenant's admin token.

Then a real turn (requires `operator`):

```bash
curl -N -X POST https://blackbox.example.com/api/chat \
  -H "authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"message":"What caused ERR_POOL_EXHAUSTED in checkout-service?"}'
```

You should see `node` events, then ground-checked `token` events, then `final`.

---

## 5. Verify it is ready

```bash
curl -fsS https://blackbox.example.com/health          # liveness
curl -fsS https://blackbox.example.com/health/ready    # readiness, per dependency
```

`/health/ready` returns `200` with:

```json
{
  "status": "ok",
  "storage": "postgres",
  "runtime": "ready",
  "cache": "ok",
  "dependencies": {
    "storage": "ok", "runtime": "ready", "retrieval": "ready",
    "llm": "ready", "cache": "ok"
  }
}
```

- `storage` is a real `SELECT 1`, not a cached flag.
- `runtime` goes `cold → ready` after warm-up; readiness retries warm-up while cold.
- `cache: "degraded"` means Redis is down and the in-process fallback is serving. Chat
  still works; shared rate limits and quotas are per-pod until Redis returns.

`GET /metrics` is unauthenticated and intended for the monitoring network only. It exports
`blackbox_http_*`, `blackbox_sse_active_streams`, retrieval/LLM/job metrics and
`blackbox_runtime_ready` / `blackbox_dependency_up`.

---

## 6. Auth and tenancy

Send `Authorization: Bearer <jwt>`. The token must carry:

| Claim | Accepted keys | Example |
|---|---|---|
| tenant | `tenant_id`, `tid`, `org_id` | `"acme"` |
| roles | `roles`, `role`, `scope` (string or list) | `["operator"]` |

| Role | Can do |
|---|---|
| `viewer` | read conversations, traces, memory, budget |
| `operator` | all of the above + `POST /api/chat` |
| `admin` | all of the above + ingest, eval, retention, offboarding |
| `platform_admin` | cross-tenant admin operations (retention/offboarding) |

A token without a tenant is rejected. Cross-tenant conversation reads return `404`, not
`403`, so IDs cannot be probed. Dev mode (`AUTH_ENABLED=false`) is an admin principal and is
refused in prod.

---

## 7. Scaling

| Component | Scale on | Notes |
|---|---|---|
| API | CPU, active SSE streams, p95 | Stateless; shared state in Redis/Postgres |
| Worker | Redis queue depth (KEDA) | Safe to run zero; the API executes jobs in-process too |
| Postgres | connections, IOPS | Keep `replicas × pool_size + overflow` below the server limit |
| Redis | ops/sec | `noeviction`; it is not authoritative for conversations |
| Retrieval | Qdrant/OpenSearch replicas | Per-region index names when `region.region_suffix` is on |

Above **one API replica**, externalize retrieval. In `blackbox/config/config.yaml`:

```yaml
retrieval:
  dense_backend: qdrant
  sparse_backend: opensearch
```

```dotenv
QDRANT_URL=https://qdrant.internal:6333
OPENSEARCH_HOSTS=https://search.internal:9200
OPENSEARCH_ALIAS=blackbox_chunks
```

Chroma writes to `/app/data` (the image sets `HOME=/app/data` for its ONNX model cache).
On an air-gapped host, warm one container with network access once, or pre-bake the model.

---

## 8. Security checklist

- [ ] `AUTH_ENABLED=true`, with a strong HS256 secret or an OIDC JWKS URL
- [ ] `CORS_ORIGINS` is an explicit allowlist, never `*`
- [ ] `TRUSTED_PROXY_CIDRS` matches only your ingress/edge
- [ ] TLS terminates at the edge; HTTP redirects or is private-only
- [ ] `/metrics` is not publicly reachable (NetworkPolicy or edge rule)
- [ ] Secrets come from a secret manager, not a committed `.env`
- [ ] `PII_REDACTION_ENABLED=true`; `QUERY_LOG_MODE=hash` unless policy says otherwise
- [ ] Postgres backups + PITR enabled
- [ ] For RLS: a non-owner application role, `RLS_ENABLED=true`, then verify that a
      cross-tenant query fails. Without `FORCE`/a non-owner role the policies are inert.
- [ ] `TENANT_QUOTA_ENABLED=true` and a per-tenant daily token/call/dollar budget
- [ ] Trace archive configured if retention runs (`TRACE_ARCHIVE_BACKEND=file|s3`)

---

## 9. Operations

**Upgrade.** Build/pull the new image tag, then redeploy. The migration service (Compose) or
the pre-upgrade hook (Helm) runs first; new API replicas only start after it succeeds.
Roll back by pinning the previous `BLACKBOX_TAG`/image. Migrations are forward-only, so a
rollback across a schema change needs the database restore path.

**Retention and archiving.** Old turns are pruned only after they are archived when an
archive backend is configured:

```bash
curl -X POST 'https://blackbox.example.com/api/admin/retention?days=90&archive=true' \
  -H "authorization: Bearer $ADMIN_TOKEN"
# queue it instead: &background=true -> 202 + job id
```

**Cost.** `GET /api/conversations/{id}/cost` and `GET /api/admin/cost` report per-conversation
and per-tenant cost from the per-node token usage stored in each trace.

**Backups.** Postgres is the source of truth for conversations, turns and tenant regions.
Redis is non-authoritative (caches, rate limits, idempotency, pending jobs); losing it loses
queued work but not data. Back up the trace archive bucket if you use S3.

---

## 10. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Process exits at startup: `AUTH_ENABLED must be true in prod` | Prod validation | Set `AUTH_ENABLED=true` and a JWT secret/JWKS URL |
| `OPENAI_API_KEY required in prod` | No provider key | Set the key, or switch `LLM_PROVIDER` |
| `CORS_ORIGINS must not contain '*' in prod` | Wildcard CORS | Use an explicit origin list |
| Readiness `503`, `storage: down` | DB unreachable / wrong URL | Check `DATABASE_URL`, network, credentials |
| Readiness `503`, `runtime: cold` | Warm-up failing | Check the logs for `readiness_warmup_failed`; `GET /metrics` shows `blackbox_runtime_ready 0` |
| Readiness `degraded`, `cache: degraded` | Redis down | Chat still works; restore Redis for shared limits/queues |
| `421` with `X-Blackbox-Home-Region` | Tenant pinned to another region | Route the request to that region's endpoint |
| First chat is slow, then normal | Cold start | Warm-up runs at startup; ensure `start_period` covers it |
| SSE arrives in one burst | A proxy is buffering | Set `flush_interval -1` in Caddy / disable buffering in the Ingress |
| Worker does nothing | No shared Redis, so it sees only its own memory | Set `REDIS_URL` for API and worker |
| Ingest says `skipped` but search is empty | Vector store was reset, ledger survived | Re-run with `{"refresh": true}` |
| Chroma/ONNX error on first start | `HOME` not writable by uid 10001 | Use the shipped image (`HOME=/app/data`) or set it |

---

## 11. Quick verification script

```bash
BASE=https://blackbox.example.com
curl -fsS "$BASE/health"
curl -fsS "$BASE/health/ready"
curl -fsS -H "authorization: Bearer $TOKEN" "$BASE/api/conversations/<id>"
python scripts/smoke_probe.py --base-url "$BASE"
```

For the Kubernetes deployment, `deploy/probes/synthetic-probe-cronjob.yaml` runs the probe on
a schedule. For failure drills and failover, see `docs/on-call-runbook.md`,
`docs/failover-runbook.md` and `docs/tenant-move-runbook.md`.
