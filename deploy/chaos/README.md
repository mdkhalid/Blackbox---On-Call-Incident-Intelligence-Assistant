# Failure injection (Chaos Mesh)

These manifests are the Phase 4 failure-injection suite. They are reference experiments,
not something to run against a production cluster without a change window.

## Prerequisites

- Chaos Mesh installed in the cluster.
- Blackbox deployed in the `blackbox` namespace with the production values.
- A Grafana dashboard or `kubectl argo rollouts` watch open, plus the alert rules applied.

## Experiments

| File | What it does | Expected behaviour |
|---|---|---|
| `pod-kill.yaml` | Kills one API pod | No failed requests; the SSE stream is retried by the client; pod count recovers; no alert |
| `pod-failure.yaml` | Makes one API pod not-ready for 2m | Readiness removes it from the Service; in-flight SSE drains within the grace period; HPA replaces it |
| `redis-latency.yaml` | Adds 100ms to egress to Redis | Rate limiting and idempotency still work; p95 turn latency rises but stays under 8s |
| `provider-latency.yaml` | Adds 2s to egress to the LLM provider | Circuit breaker may open after the threshold; retries with jitter; LLM error ratio stays below 2% |
| `az-failure.yaml` | Partitions one zone's API pods from the cluster | Remaining zones keep serving; only requests pinned to the isolated zone fail; PDB keeps 2 replicas |
| `store-failure.yaml` | Fails a retrieval-store pod for 5m | Chat degrades to an honest failure answer, not a 500; retrieval recovers when the pod returns |
| `provider-outage.yaml` | Partitions all API pods from the LLM providers | Breaker opens; turns return honest failures; the circuit closes when the partition is removed |
| `region-partition.yaml` | Partitions EU pods from US pods (two-region reference) | Reads stay regional; writes for a tenant whose home region is unreachable are rejected with 421, never written in the wrong region |

## Success criteria

- `BlackboxApiDown` and `BlackboxErrorBudgetFastBurn` do not fire.
- `blackbox:http_request_duration_p95_5m` stays below 8s.
- `BlackboxJobQueueBacklog` clears within 5 minutes of the experiment ending.
- No cross-tenant data appears in the trace viewer (manual check).
- During `provider-outage.yaml`, a chat turn returns the honest-failure path
  (`status: insufficient_evidence`) rather than a fabricated precedent.
- During `region-partition.yaml`, no conversation is written to a non-home region.

## Verdicts

`scripts/chaos_verify.py` probes `/health`, `/health/ready`, `/metrics` and one chat
turn, and exits non-zero if the documented criteria are not met. Run it while an
experiment is active and again after deleting it, so a drill has a machine-checkable
verdict rather than a screenshot.

## Run

```bash
kubectl apply -f deploy/chaos/
kubectl get podchaos,networkchaos -n blackbox
# ... observe ...
kubectl delete -f deploy/chaos/
```