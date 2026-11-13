# Blackbox Helm chart

Deploys the Blackbox API (FastAPI + SSE), the background-job worker, database
migrations, autoscaling and policy for a production Kubernetes cluster.

## Prerequisites

- Kubernetes ≥ 1.27, Helm ≥ 3.14
- A Postgres database (Aurora/RDS or self-managed) reachable from the pods
- Redis for shared rate limits, idempotency, the job queue and caches
- For a true multi-pod deployment: Qdrant (dense) and OpenSearch (sparse), because
  the default Chroma/BM25 adapters are node-local. See the production document.
- An OIDC issuer, or a shared `AUTH_JWT_SECRET`, plus `AUTH_ENABLED=true`
- Optional: KEDA (worker autoscaling), Prometheus Operator (ServiceMonitor),
  an ingress controller with SSE-capable proxy settings

## Install

```bash
kubectl create namespace blackbox

# Create the secret through your secret manager, then reference it:
helm upgrade --install blackbox deploy/helm/blackbox \
  -n blackbox \
  -f deploy/helm/blackbox/values-prod.example.yaml \
  --set existingSecret=blackbox-secrets
```

The Secret must provide the sensitive environment variables, at minimum:

| Key | Required in prod |
|---|---|
| `DATABASE_URL` | yes |
| `REDIS_URL` | yes (shared queue/limits) |
| `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` | yes |
| `AUTH_JWT_SECRET` or `AUTH_JWKS_URL` | yes (with `AUTH_ENABLED=true`) |
| `OPENSEARCH_PASSWORD`, `QDRANT_API_KEY` | if those backends need auth |

Non-secret settings live in `config` and are rendered into a ConfigMap. Helm
restarts the pods on a ConfigMap change via a checksum annotation.

## What is deployed

| Object | Purpose |
|---|---|
| `Deployment/<release>` | API pods; startup `/health`, readiness `/health/ready` (real store check), liveness `/health` |
| `Deployment/<release>-worker` | `python -m blackbox.worker`; consumes ingest/eval/retention jobs from Redis |
| `Job/<release>-migrate` | Helm pre-install/pre-upgrade hook running `alembic upgrade head`; a failure aborts the release and leaves the old pods serving |
| `Service` | ClusterIP on port 80 → container port 8000 |
| `Ingress` (optional) | SSE annotations: proxy buffering off, 600s read/send timeout, 32 KiB body limit |
| `HorizontalPodAutoscaler` (optional) | CPU by default; override `api.autoscaling.metrics` to scale on `blackbox_sse_active_streams` via prometheus-adapter |
| `ScaledObject` (optional) | KEDA scales the worker on the Redis pending-list length per job kind |
| `PodDisruptionBudget` | API `minAvailable: 2`; worker `maxUnavailable: 1` |
| `NetworkPolicy` (optional) | API ingress from ingress/monitoring namespaces; DNS and explicit egress allowlist |
| `ServiceMonitor` (optional) | Prometheus Operator scrape of `/metrics` |
| Helm test pod | `helm test <release>` curls `/health` through the Service |

## Scaling notes

- **API:** keep ≥ 3 replicas across zones (`topologySpreadConstraints` does this by
  default). CPU is a poor proxy for SSE load; once prometheus-adapter is available,
  scale on `blackbox_sse_active_streams` as well. Scale-down uses a 300s
  stabilization window so long turns are not cut mid-stream.
- **Turn safety:** one active turn per conversation is enforced by a Postgres
  advisory lock, so any replica can serve any request. Per-tenant *admission*
  control is still per pod; global quotas are a tracked Phase 4 item.
- **Worker:** with Redis the queue is shared, so KEDA's Redis scaler is the correct
  signal. Without `REDIS_URL` the worker sees only its own process and is useless —
  the chart still deploys it, and the process logs a warning.
- **Retrieval:** Chroma and in-process BM25 are node-local. A multi-replica install
  must set `QDRANT_URL` + `OPENSEARCH_HOSTS` and the matching YAML backends; doing
  so is a values/config change, not a chart change.

## SSE and ingress

Buffering is the default failure mode for SSE. The chart sets the nginx annotations
that disable it. For an AWS ALB use, for example:

```yaml
alb.ingress.kubernetes.io/target-type: ip
alb.ingress.kubernetes.io/load-balancer-attributes: idle_timeout.timeout_seconds=600
```

The app's `TRUSTED_PROXY_CIDRS` must contain the ingress/load-balancer networks,
otherwise the rate limiter keys every client on the proxy address.

## Verification

```bash
helm lint deploy/helm/blackbox \
  -f deploy/helm/blackbox/values-prod.example.yaml
helm template blackbox deploy/helm/blackbox -n blackbox \
  -f deploy/helm/blackbox/values-prod.example.yaml | less
helm test blackbox -n blackbox
kubectl port-forward -n blackbox svc/blackbox 8000:80
curl -fsS localhost:8000/health/ready
curl -fsS localhost:8000/metrics | head
```

## Known limits of this chart

- No bundled Postgres/Redis/OpenSearch/Qdrant. Managed stores are the target; the
  Terraform reference is a Phase 4 deliverable.
- The migration hook assumes the `alembic` CLI and `alembic/` directory are in the
  image (the repository Dockerfile ships them).
- NetworkPolicy defaults are disabled because the allowlist is cluster-specific.
  Enable them via the prod example and verify with `kubectl exec` before relying on
  them.
