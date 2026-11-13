# Observability

The API exposes Prometheus metrics at `GET /metrics` in the text exposition format.
The worker has no HTTP surface; scale it from Redis queue depth (KEDA in the chart)
and read its structured logs for job outcomes.

## Scrape

With kube-prometheus-stack, enable the chart's ServiceMonitor:

```yaml
serviceMonitor:
  enabled: true
  labels:
    release: kube-prometheus-stack
```

Without the operator, add a static job:

```yaml
scrape_configs:
  - job_name: blackbox
    metrics_path: /metrics
    static_configs:
      - targets: ["blackbox.blackbox.svc:80"]
```

The endpoint is intentionally unauthenticated: Prometheus cannot present a user JWT.
Restrict it with a NetworkPolicy that admits only the monitoring namespace (the prod
values example does this).

## Metric contract

| Metric | Type | Labels | Use |
|---|---|---|---|
| `blackbox_build_info` | gauge | `version`, `env` | Deployment identity; absence means the service is unreachable |
| `blackbox_http_requests_total` | counter | `method`, `path`, `status` | Error ratio / SLO burn. `path` is the **route template**, never a raw id |
| `blackbox_http_request_duration_seconds` | histogram | `method`, `path` | Turn latency; `/api/chat` includes the whole SSE stream |
| `blackbox_sse_active_streams` | gauge | — | Concurrency; custom-metric HPA input |
| `blackbox_retrieval_duration_seconds` | histogram | `stage` (`dense`, `sparse`, `fuse`, `rerank`, `cache`, `total`) | Retrieval SLO and store diagnosis |
| `blackbox_retrieval_cache_events_total` | counter | `result` (`hit`, `miss`) | Cache hit ratio |
| `blackbox_llm_calls_total` | counter | `node`, `model`, `cached` | Provider traffic and cost |
| `blackbox_llm_call_duration_seconds` | histogram | `node` | Provider latency by graph node |
| `blackbox_llm_tokens_total` | counter | `node`, `kind` (`prompt`, `completion`) | Token/cost attribution |
| `blackbox_llm_errors_total` | counter | `node` | Failures after retries |
| `blackbox_job_queue_depth` | gauge | `kind` | Worker backlog; read at scrape time |

Labels are deliberately bounded: the HTTP path is the route template and the model
name is the configured model, so no conversation id, query or tenant value becomes a
time series. Tenant-level cost reporting is a Phase 5 item and must use a bounded
aggregation, not a per-tenant label on these counters.

## Alerts

```bash
kubectl apply -n monitoring -f deploy/observability/prometheus-rules.yaml
```

The rules implement the documented SLOs: 99.9% availability (multi-window 14.4× /
6× error-budget burn), complete-answer p95 ≤ 8s, retrieval p95 ≤ 400ms, LLM error
ratio, job backlog and an informational "no chat traffic" signal.

Grafana dashboards and the on-call runbooks are tracked in the development tracker;
the metric names above are the stable contract they will be built against.