# AWS reference infrastructure

A self-contained Terraform root module for the production shape described in
[`docs/scaling-and-production-readiness.md`](../../docs/scaling-and-production-readiness.md):
an EKS cluster, Aurora PostgreSQL, ElastiCache Redis, an S3 trace archive, KMS
keys, Secrets Manager and the IRSA roles the chart and External Secrets use.

It is a **reference**, not a turnkey product: it encodes choices (one region,
Aurora provisioned, Redis cluster mode off, no Qdrant/OpenSearch) and documents
what to change. There are no external registry modules, so `terraform validate`
works offline and the whole plan is readable in this directory.

## What it creates

| Area | Resources |
|---|---|
| Network | VPC, 3 private + 3 public subnets across AZs, one NAT per AZ (configurable), route tables, S3 gateway endpoint, optional interface endpoints, VPC flow logs to CloudWatch |
| EKS | Control plane with all audit log types, private endpoint by default, secrets encrypted with a CMK, managed node groups (`system` and tainted `batch`), AL2023 nodes with IMDSv2 required and encrypted gp3 root volumes, managed add-ons (VPC CNI, kube-proxy, CoreDNS, EBS CSI) |
| IAM | Cluster/node roles, EBS CSI IRSA, cluster access entries for named admins, app IRSA role (trace archive only), External Secrets IRSA role (read secrets only) |
| Database | Aurora PostgreSQL 16, 1 writer + N-1 readers, RDS-managed master password in Secrets Manager, CMK storage encryption, Performance Insights, enhanced monitoring, PostgreSQL log export, 14-day PITR, deletion protection |
| Cache | ElastiCache Redis 7 with TLS in transit, at-rest encryption, one replica, Multi-AZ failover, `noeviction`, slow-log export |
| Storage | Versioned, KMS-encrypted S3 bucket for traces with lifecycle to Standard-IA/Glacier IR and expiry; optional access-log bucket |
| Secrets | CMK-encrypted `app` secret (JWT signing key, optional provider key) and `redis` secret (`REDIS_URL`, `REDIS_PASSWORD`) |

## What it deliberately does not create

- **Qdrant / OpenSearch.** The Phase 2 benchmark (Qdrant+OpenSearch vs
  OpenSearch-only) needs live clusters and is still open. Running stateful
  search stores is also a capacity decision, not a checkbox. Add them here once
  the benchmark decides; the chart already accepts `QDRANT_URL` and
  `OPENSEARCH_HOSTS`.
- **Ingress, DNS, certificates, monitoring, KEDA, External Secrets.** These are
  cluster add-ons with their own charts (ingress-nginx or AWS Load Balancer
  Controller, cert-manager, kube-prometheus-stack, KEDA, external-secrets).
  Helm installs them; this module only provides the IAM and network they need.
  A full platform bootstrap belongs in a GitOps repository.
- **WAF / CloudFront.** Front the ingress with them when the public hostname is
  decided; they are edge concerns, not cluster concerns.
- **A second region.** Phase 5 (multi-region/DR) reuses this module per region
  and adds the global DNS and replication layer.

## Prerequisites

- Terraform ≥ 1.7, AWS credentials with permission to create the resources above
- A remote state bucket + lock table (see `versions.tf`; local state is not safe)
- `kubectl`, `helm`, and the add-ons listed above for the cluster side
- The container image published somewhere the nodes can pull from (ECR is the
  obvious choice; it is not built here)

## Validate

```bash
cd deploy/terraform
terraform fmt -check -recursive
terraform init -backend=false
terraform validate

# Security scan of the HCL (the CI job runs the same with exit-code 1)
trivy config --severity HIGH,CRITICAL .
```

The module was validated with Terraform 1.14 and AWS provider 5.100; `trivy
config` reports no HIGH/CRITICAL findings. **It has not been applied**: this
environment has no AWS account. Treat the first `terraform plan` as a review,
not a formality, especially for the EKS version (standard support is 14 months)
and Aurora engine-version availability in your region.

## Apply

```bash
cd deploy/terraform
terraform init \
  -backend-config="bucket=example-tfstate" \
  -backend-config="key=blackbox/prod/terraform.tfstate" \
  -backend-config="region=eu-west-1" \
  -backend-config="dynamodb_table=example-tfstate-lock" \
  -backend-config="encrypt=true"

cp terraform.tfvars.example terraform.tfvars   # then edit
terraform plan -out=tfplan
terraform apply tfplan

aws eks update-kubeconfig --name "$(terraform output -raw cluster_name)" \
  --region "$(terraform output -raw region)"
kubectl get nodes -L role
```

Uncomment `backend "s3" {}` in `versions.tf` and initialise with `-backend-config`
as above; the block is left empty on purpose so the bucket and table are not
hard-coded.

## Wire the chart to the stack

```bash
helm upgrade --install blackbox deploy/helm/blackbox \
  -n blackbox --create-namespace \
  -f deploy/helm/blackbox/values-prod.example.yaml \
  --set existingSecret=blackbox-secrets \
  --set serviceAccount.annotations."eks\.amazonaws\.com/role-arn"="$(terraform -chdir=deploy/terraform output -raw app_irsa_role_arn)" \
  --set config.TRACE_ARCHIVE_BACKEND=s3 \
  --set config.TRACE_ARCHIVE_BUCKET="$(terraform -chdir=deploy/terraform output -raw trace_archive_bucket)" \
  --set config.TRACE_ARCHIVE_REGION="$(terraform -chdir=deploy/terraform output -raw region)" \
  --set worker.keda.enabled=true \
  --set worker.keda.address="$(terraform -chdir=deploy/terraform output -raw redis_primary_endpoint):6379" \
  --set worker.keda.tls=true \
  --set worker.keda.authenticationRef=blackbox-redis-auth
```

Then:

1. Create the ExternalSecret from
   [`deploy/external-secrets`](../external-secrets) so `blackbox-secrets` exists.
2. Put the worker on the batch nodes:

   ```yaml
   worker:
     nodeSelector:
       role: batch
     tolerations:
       - key: dedicated
         value: batch
         effect: NoSchedule
   ```

3. Create the KEDA TriggerAuthentication (the password is in the app Secret,
   key `REDIS_PASSWORD`):

   ```yaml
   apiVersion: keda.sh/v1alpha1
   kind: TriggerAuthentication
   metadata:
     name: blackbox-redis-auth
     namespace: blackbox
   spec:
     secretTargetRef:
       - parameter: password
         name: blackbox-secrets
         key: REDIS_PASSWORD
   ```

4. Only reach the API through a VPN/bastion while
   `cluster_endpoint_public_access = false`, or set it to `true` with an explicit
   CIDR allowlist.

## Capacity notes

- `system` nodes run the API pods. The chart's HPA adds pods; the node group
  maximum (9 × m6i.large by default) bounds it. Watch pod CIDR exhaustion:
  VPC CNI allocates a secondary IP per pod unless prefix delegation is enabled.
  If you expect > 30 pods per node, enable `ENABLE_PREFIX_DELEGATION` in the
  `vpc-cni` add-on configuration.
- `batch` nodes are tainted. KEDA adds worker pods; the cluster autoscaler (or
  EKS managed node group scaling) adds nodes. The chart's `maxReplicaCount` and
  `batch_node_max` must be raised together.
- Redis holds the job queue: size it by queue depth and payload size, not by
  cache hit ratio. `noeviction` is deliberate — eviction would silently drop
  jobs.
- Aurora: the API opens a pool per pod. Keep `maxReplicaCount × pool size` below
  the cluster's `max_connections`; r6g.large starts around 1,700 with the
  default parameter group.

## Security decisions in this module

| Decision | Why |
|---|---|
| No public Kubernetes endpoint by default | The API server is the highest-value target; a VPN/bastion is cheaper than a compromise |
| Kubernetes Secrets encrypted with a CMK | etcd at rest is not enough when snapshots leave the cluster |
| Two separate IRSA roles (app vs External Secrets) | A compromised pod can write traces or read secrets, not both |
| No static AWS keys in pods | IRSA only; the chart's ServiceAccount is annotated, not given credentials |
| HTTPS-only egress plus the VPC CIDR | Blocks the common "exfiltrate over 53/8080" path while keeping image pulls and LLM calls working; the two 0.0.0.0/0 rules are HTTPS and carry an inline Trivy justification |
| `noeviction` on Redis | The queue and idempotency keys share the instance with caches |
| RDS-managed master password | No password in state or a tfvars file; rotation is an RDS operation |
| S3 versioning on the trace bucket | A retried archive write cannot destroy the only copy of a turn |
| Deletion protection + final snapshot | The database survives a careless `terraform destroy` |

## Operations

- **Backups:** Aurora PITR is 14 days by default; Redis snapshots are 5 days.
  Run a restore drill into a scratch cluster before trusting either.
- **Rotation:** RDS rotates the master credential; External Secrets refreshes
  the Kubernetes Secret, but pods read `DATABASE_URL` at start-up. See
  [`deploy/external-secrets/README.md`](../external-secrets/README.md) for the
  restart step. Rotating `AUTH_JWT_SECRET` invalidates all tokens.
- **Cost:** the default shape (3 × m6i.large, 1 × m6i.xlarge, 2 × db.r6g.large,
  1 × cache.r7g.large + replica, 3 NAT gateways) is a few hundred dollars a
  month before data transfer and LLM spend. `single_nat_gateway = true` and one
  Aurora reader cut it for staging.
- **Destroy:** `database_deletion_protection = true` and versioned buckets make
  destroy a deliberate two-step; that is the point.

## Next steps

1. Add OpenSearch/Qdrant here after the backend benchmark decision.
2. Add the platform add-ons through GitOps (Argo CD or Flux), not Terraform.
3. Add a CloudWatch dashboard/alarm module or export the metric alarms through
   the existing Prometheus rules — the SLO contract lives in
   [`deploy/observability`](../observability).
4. Repeat per region for Phase 5, with cross-region S3 replication and Aurora
   Global Database.
