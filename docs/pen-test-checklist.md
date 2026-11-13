# Penetration test checklist

Run before each production release and after any change to auth, tenancy, retrieval,
the graph or the deployment. Record the tester, date and commit in the tracker.

## Identity and access

- [ ] JWT validation rejects expired, unsigned, wrong-issuer and wrong-audience tokens.
- [ ] Role checks enforce `viewer` < `operator` < `admin` on every route; `/api/admin/*` and
      `/api/eval/*` are admin-only.
- [ ] Tenant binding: a token for tenant A cannot read or write tenant B conversations,
      traces, memory or retrieved chunks (cross-tenant matrix is a release blocker).
- [ ] `AUTH_ENABLED=false` is rejected in prod by settings validation.
- [ ] No endpoint leaks stack traces or internal paths in error responses.

## Input and prompt injection

- [ ] Oversized bodies are rejected with 413 before JSON parsing (`BodyLimitMiddleware`).
- [ ] Prompt-injection flags are logged; strict mode blocks the request.
- [ ] Retrieved chunks are treated as data: the generate prompt delimits evidence and the
      ground check strips unsupported claims.
- [ ] Secret scrubbing removes configured secret patterns from answers and traces.

## Rate limiting and quotas

- [ ] Per-client rate limit is keyed on the resolved client IP, not the TCP peer.
- [ ] `X-Forwarded-For` is ignored unless the peer is a trusted proxy.
- [ ] Per-tenant admission control returns 429 with `Retry-After`.
- [ ] Global per-tenant token and LLM-call quotas (when enabled) are shared across pods
      and fail open if Redis is down.
- [ ] Idempotency keys replay the stored result and reject an in-flight duplicate with 409.

## Data isolation

- [ ] Retrieval filters on `tenant_id` are mandatory and cannot be overridden by the caller.
- [ ] Postgres RLS policies (where enabled) deny cross-tenant rows.
- [ ] Trace and archive reads are tenant-scoped.
- [ ] Tenant offboarding removes chunks, conversations, entities and archived objects.

## Transport and headers

- [ ] TLS is enforced at the edge; HSTS and secure cookies where applicable.
- [ ] CORS is an explicit allowlist; `*` is rejected in prod.
- [ ] Security headers (CSP, X-Frame-Options, X-Content-Type-Options) are set by the
      ingress or the app.
- [ ] `/metrics` is NetworkPolicy-restricted to the monitoring namespace.

## Secrets and supply chain

- [ ] No secret appears in images, logs, metrics labels or traces.
- [ ] Images are scanned (Trivy) and shipped with an SBOM; base image is current.
- [ ] Service accounts are least-privilege; `automountServiceAccountToken: false`.
- [ ] Pods run as uid/gid 10001, non-root, with dropped capabilities and seccomp.

## Infrastructure

- [ ] NetworkPolicy default-deny is verified with `kubectl exec` from a foreign namespace.
- [ ] EKS endpoint is private; secrets are encrypted with a CMK.
- [ ] Aurora and ElastiCache are not publicly reachable.
- [ ] S3 trace archive is versioned and encrypted; public access blocked.
- [ ] IRSA roles are split: app writes traces only, External Secrets reads secrets only.

## Observability and recovery

- [ ] Alerts fire on a synthetic failure (see `deploy/chaos/`).
- [ ] Backup restore drill completed within the RPO.
- [ ] Graceful shutdown drains SSE streams within the termination grace period.
- [ ] Migration rollback (`alembic downgrade`) is rehearsed in staging.