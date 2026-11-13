# On-call runbook

## First 5 minutes

1. Acknowledge the page and open the Grafana **Blackbox overview** dashboard.
2. Check `BlackboxApiDown` and the instance count: is the service reachable at all?
3. Check `BlackboxErrorBudgetFastBurn`: is this a real regression or a probe blip?
4. Check recent deploys in Argo CD / CI; if a deploy landed in the last 30 minutes,
   roll back first and investigate second.
5. Run the synthetic probe manually:
   `python scripts/smoke_probe.py --base-url https://blackbox.example.com --chat`

## Common alerts

| Alert | Likely cause | Action |
|---|---|---|
| `BlackboxApiDown` | Pod crash, bad deploy, network policy | Check pod status, recent deploy, rollback |
| `BlackboxErrorBudgetFastBurn` | Regression in a recent deploy | Roll back, then bisect |
| `BlackboxAnswerLatencySloBreach` | LLM provider slow, retrieval store slow | Check provider status page, retrieval p95 by stage, queue depth |
| `BlackboxRetrievalSloBreach` | OpenSearch/Qdrant degradation | Check store health, index size, cache hit ratio |
| `BlackboxLlmErrorRateHigh` | Provider 429/5xx, bad key, quota | Check provider status, key validity, circuit breaker state in logs |
| `BlackboxJobQueueBacklog` | Worker down or stuck job | Check worker pods, job status endpoint, Redis queue depth |
| `BlackboxNoSseTraffic` | Ingress misconfiguration, client outage | Check ingress, TLS, client dashboards |

## Mitigations

- **Roll back a deploy**: `kubectl argo rollouts undo blackbox -n blackbox` or revert the commit
  in Git and let Argo CD sync.
- **Drain a bad pod**: `kubectl delete pod -n blackbox <pod>`; the PDB keeps at least 2 ready.
- **Disable a noisy feature**: set the relevant env var (for example `OTEL_ENABLED=false`) in
  the ConfigMap and restart; the chart restarts pods on a ConfigMap checksum change.
- **Fail open on Redis**: rate limiting and quotas degrade to per-pod/local behaviour; chat
  keeps working. Fix Redis, then restart to re-establish the shared counters.
- **Fail open on the LLM**: the circuit breaker opens after the configured threshold; the
  graph returns an honest failure instead of a 500. Check provider status before forcing
  traffic.

## Escalation

- L1: on-call engineer, 15-minute response.
- L2: service owner, 30-minute response for SLO burn.
- L3: platform/infra for cluster, database or Redis incidents.

## Post-incident

- Capture the trace id from the failing request and pull the full trace in Tempo.
- Run the relevant Chaos Mesh experiment from `deploy/chaos/` in staging to reproduce.
- Update the pen-test checklist or runbook if a new failure mode was found.