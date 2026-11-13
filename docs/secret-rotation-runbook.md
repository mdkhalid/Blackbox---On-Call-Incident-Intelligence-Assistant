# Secret rotation runbook

Rotate one secret at a time, verify, then move to the next. Never rotate two secrets in
the same change window unless one depends on the other.

## General procedure (External Secrets)

1. Update the value in AWS Secrets Manager (or your secret manager).
2. Wait for the ExternalSecret to sync, or force it:
   `kubectl annotate externalsecret blackbox -n blackbox force-sync=$(date +%s) --overwrite`
3. Verify the Kubernetes Secret changed:
   `kubectl get secret blackbox-secrets -n blackbox -o jsonpath='{.data}' | ...`
4. Restart the pods so they read the new value:
   `kubectl rollout restart deployment/blackbox -n blackbox`
   (and the worker). With a Rollout, use `kubectl argo rollouts restart blackbox -n blackbox`.
5. Watch `/health/ready` and the SLO alerts for 15 minutes.

## JWT signing secret / JWKS

- Rotating `AUTH_JWT_SECRET` (HS256) invalidates every existing session: users must
  log in again. Schedule it out of peak hours.
- For JWKS (RS256), publish the new key under a new `kid`, keep the old key valid for
  the overlap window (at least the longest token lifetime), then remove the old key.
- Verify with a token issued before rotation: it must be rejected after the old key is
  removed and accepted during the overlap.

## LLM provider API key

- Create the new key in the provider console, add it to Secrets Manager, sync and restart.
- Keep the old key active until the rollout is complete and a chat turn succeeds.
- Revoke the old key only after 24 hours of clean metrics.

## Database password

- Aurora uses the RDS-managed master password. To rotate, use Secrets Manager rotation
  (the External Secrets example maps `password` into `DATABASE_URL` via `urlquery`).
- The connection pool reads the URL at process start, so a pod restart is required.
- Run `alembic upgrade head` is not needed for a password change; the migration Job
  still runs on the next Helm upgrade.

## Redis password

- Update ElastiCache auth and the `redis` secret (`REDIS_URL` and `REDIS_PASSWORD`).
- KEDA's TriggerAuthentication references the same secret; verify the worker scales after
  the restart.
- Rate limits and idempotency keys live in Redis; a password change does not clear them.

## Trace archive (S3)

- The app uses IRSA, not a long-lived key. Rotating is an IAM policy/role change, not a
  secret change: update the role, then restart the pods so STS fetches new credentials.
- Bucket encryption uses KMS; rotating the CMK is handled by KMS and needs no app change.

## Verification

- [ ] `/health/ready` green on all pods.
- [ ] A chat turn succeeds end to end.
- [ ] `/metrics` still scrapes; no 401/403 in the app logs.
- [ ] Alert rules are not firing.