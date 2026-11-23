# Deploy anywhere — Docker Compose

The simplest production path that is not tied to a cloud. It runs the same image as
the Helm chart, with the same secrets and health checks, on:

- a plain Linux VM,
- a laptop,
- any cloud's container/VM service (ECS, Cloud Run, Fly.io, Hetzner, …),
- or a managed Kubernetes cluster, where [`deploy/helm/blackbox`](../helm/blackbox)
  is the better fit.

Nothing here requires AWS. The Terraform under [`deploy/terraform`](../terraform) is
an *optional* AWS reference; this stack only needs a Postgres URL and a Redis URL.

## What runs

| Service | Role |
|---|---|
| `migrate` | one-shot `alembic upgrade head`; API/worker wait for it |
| `api` | the FastAPI app, 2 replicas by default, no host port |
| `worker` | `python -m blackbox.worker`, consumes the shared Redis job queue |
| `edge` | Caddy — the only public port, SSE-safe proxy, load balances API replicas |
| `postgres`, `redis` | only with the `-local` override, for a single server |

## Managed stores (any cloud)

```bash
cp deploy/compose/.env.prod.example deploy/compose/.env.prod
# fill in DATABASE_URL, REDIS_URL, AUTH_JWT_SECRET, OPENAI_API_KEY
docker compose -f deploy/compose/docker-compose.prod.yml \
  --env-file deploy/compose/.env.prod up -d
```

## All-in-one server / local

```bash
cp deploy/compose/.env.prod.example deploy/compose/.env.prod
# uncomment the local DATABASE_URL / REDIS_URL and set POSTGRES_PASSWORD
docker compose -f deploy/compose/docker-compose.prod.yml \
  -f deploy/compose/docker-compose.prod-local.yml \
  --env-file deploy/compose/.env.prod up -d
```

## Scaling

The API has no published host port, so replicas are safe to add:

```bash
docker compose -f deploy/compose/docker-compose.prod.yml \
  --env-file deploy/compose/.env.prod up -d --scale api=4 --scale worker=2
```

Readiness (`/health/ready`) is per dependency — database, runtime, cache, retrieval
and LLM — so a replica is not routed traffic until it can actually serve a turn.
The worker scales independently and is safe to run at zero (the API also executes
jobs in-process).

> **Above one API replica, externalize retrieval.** Chroma and the in-process BM25
> index are node-local. Set `retrieval.dense_backend=qdrant` +
> `retrieval.sparse_backend=opensearch`, and point `QDRANT_URL` /
> `OPENSEARCH_HOSTS` at managed services. The Helm chart and Terraform reference show
> the same shape for Kubernetes.

> **First start downloads Chroma's ONNX MiniLM model (~80 MB)** into
> `/app/data/.cache/chroma`. The image sets `HOME=/app/data` so the unprivileged user
> can write it. On an air-gapped host, start one container once with network access, or
> pre-bake the model into the image, before scaling replicas.

## TLS and the edge

Caddy serves either:

- `BLACKBOX_DOMAIN=:80` — plain HTTP, for a private network or a load balancer that
  terminates TLS itself; or
- `BLACKBOX_DOMAIN=blackbox.example.com` — automatic HTTPS with a public DNS record.

`Caddyfile` sets `flush_interval -1` so SSE node/token events are not buffered. If
you put another proxy in front, mirror that setting and set `TRUSTED_PROXY_CIDRS` to
its network so rate limiting sees the real client address.

## Operations

```bash
# health
curl -fsS http://localhost/health/ready

# metrics (not proxied publicly; scrape it from the Docker network or add a rule)
docker compose -f deploy/compose/docker-compose.prod.yml exec api \
  curl -fsS http://127.0.0.1:8000/metrics

# logs
docker compose -f deploy/compose/docker-compose.prod.yml logs -f api worker

# upgrade: pull/build the new image, then let `migrate` run again
docker compose -f deploy/compose/docker-compose.prod.yml \
  --env-file deploy/compose/.env.prod up -d --build
```

The migration is a separate service, so a failed upgrade aborts before new API
replicas receive traffic. Roll back by pinning `BLACKBOX_TAG` to the previous image.
