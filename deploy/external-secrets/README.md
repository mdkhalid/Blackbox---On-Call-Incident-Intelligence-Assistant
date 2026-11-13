# External Secrets

The Helm chart never renders secret values in production. It expects a Secret
named by `existingSecret` (default in the prod example: `blackbox-secrets`) with
these keys:

| Key | Source |
|---|---|
| `DATABASE_URL` | Composed from the RDS-managed master credential |
| `REDIS_URL` | Terraform `blackbox-<env>/redis` (includes the auth token; TLS only) |
| `REDIS_PASSWORD` | Same secret; KEDA's Redis scaler cannot parse a URL |
| `AUTH_JWT_SECRET` | Terraform `blackbox-<env>/app` |
| `OPENAI_API_KEY` (or `ANTHROPIC_API_KEY`) | Terraform `blackbox-<env>/app` when `openai_api_key` is set, otherwise inject it yourself |
| `OPENSEARCH_PASSWORD`, `QDRANT_API_KEY` | Your search-store stack, when those backends need auth |

## ClusterSecretStore

The Terraform stack creates an IRSA role for the operator
(`external_secrets_role_arn`). Annotate the controller ServiceAccount with it and
create one store:

```yaml
apiVersion: external-secrets.io/v1beta1
kind: ClusterSecretStore
metadata:
  name: aws-secrets-manager
spec:
  provider:
    aws:
      service: SecretsManager
      region: eu-west-1
      auth:
        jwt:
          serviceAccountRef:
            name: external-secrets
            namespace: external-secrets
```

Then apply [`blackbox-externalsecret.yaml`](blackbox-externalsecret.yaml) after
substituting the real secret names.

## Rotation

RDS rotates the master password on its own schedule (secret name `rds!cluster-…`).
External Secrets refreshes the Kubernetes Secret, but the application reads
`DATABASE_URL` at start-up, so a running pod keeps using the previous password
until its connections recycle. The rotation runbook (Phase 4) must:

1. Rotate in Secrets Manager.
2. Wait for the ExternalSecret to report `SecretSynced`.
3. Roll the API and worker Deployments (`kubectl rollout restart`) so new pods
   pick up the new URL.

`AUTH_JWT_SECRET` is different: rotating it invalidates every issued token, so it
is a planned maintenance action, not an automatic one.

## Why not `secret.create`

The chart can render a Secret from values, but that puts credentials in Helm
release history and in `values.yaml` diffs. It exists for local demos only.
