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

## Success criteria

- `BlackboxApiDown` and `BlackboxErrorBudgetFastBurn` do not fire.
- `blackbox:http_request_duration_p95_5m` stays below 8s.
- `BlackboxJobQueueBacklog` clears within 5 minutes of the experiment ending.
- No cross-tenant data appears in the trace viewer (manual check).

## Run

```bash
kubectl apply -f deploy/chaos/
kubectl get podchaos,networkchaos -n blackbox
# ... observe ...
kubectl delete -f deploy/chaos/
```