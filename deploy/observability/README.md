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

## Traces

Tracing is opt-in and dependency-free until enabled. Install the `otel` extra (the
Dockerfile already does), then:

```yaml
config:
  OTEL_ENABLED: "true"
  OTEL_EXPORTER_OTLP_ENDPOINT: http://otel-collector.monitoring.svc:4318
  OTEL_SERVICE_NAME: blackbox-api
  OTEL_TRACES_SAMPLER_ARG: "0.1"
```

`OTEL_ENABLED=false`, a missing SDK or an invalid exporter config all resolve to the
no-op tracer: the service keeps serving without a collector.

| Span | Attributes | Notes |
|---|---|---|
| `http.request` | `http.method`, `http.route` (template), `http.status_code` | Pure-ASGI; stays open for the whole SSE body. Renamed to `<METHOD> <route>` after routing |
| `chat.turn` | `tenant.id`, `conversation.id`, `chat.status`, `chat.route`, `chat.answer_chars`, `chat.total_tokens`, `chat.idempotent` | One turn, including persistence and the final event |
| `graph.node.<node>` | `graph.node`, `graph.route`, `graph.chunks`, `graph.status`, `graph.error`, `graph.grade_sufficient` | router, retrieve, grade, rewrite, generate, ground_check, honest_failure, entity_lookup, direct_answer |
| `retrieval.retrieve` | `retrieval.top_k`, `retrieval.tenant_id`, `retrieval.chunks`, `retrieval.cached`, `retrieval.total_ms` | Store spans are children of the retrieve node |
| `llm.complete` | `llm.node`, `llm.model`, `llm.max_tokens`, `llm.json_mode`, token counts, `llm.cached` | Exceptions are attached to the span |
| `db.record_turn` | `db.operation`, `tenant.id`, `db.trace_events`, `db.status` | The turn write, after the graph finishes |

`structlog` binds `trace_id` (and `request_id`) for the request, so a JSON log line can
be joined to its trace. Span names are a contract: dashboards and TraceQL queries should
use these names, not free-form ones.

## Dashboards and the logs/traces stack

The directory contains a complete reference bundle:

| File | Purpose |
|---|---|
| [`grafana/dashboards/blackbox-overview.json`](grafana/dashboards/blackbox-overview.json) | Grafana dashboard: SLO/error ratio, latency, SSE concurrency, retrieval stages, LLM calls/tokens/errors, queue depth, recent traces and trace-correlated logs |
| [`grafana/provisioning/datasources/datasources.yaml`](grafana/provisioning/datasources/datasources.yaml) | Prometheus, Loki and Tempo datasources; Loki `trace_id` → Tempo link and Tempo → Loki/metrics links |
| [`grafana/provisioning/dashboards/dashboards.yaml`](grafana/provisioning/dashboards/dashboards.yaml) | File provider for the dashboard JSON (mount at `/var/lib/grafana/dashboards`) |
| [`otel-collector.yaml`](otel-collector.yaml) | OTLP receiver, memory limiter, batching, cluster label and Tempo exporter |
| [`tempo.yaml`](tempo.yaml) | Single-binary Tempo with local blocks, 24h retention and service-graph generation |
| [`loki.yaml`](loki.yaml) | Single-binary Loki with TSDB/filesystem storage, 7-day retention and structured metadata |
| [`promtail-values.yaml`](promtail-values.yaml) | Promtail Helm values: Blackbox namespace only, JSON fields as labels, `trace_id` extracted |

Apply the rules and feed the stack configs to the charts (or mount them as ConfigMaps):

```bash
kubectl apply -n monitoring -f deploy/observability/prometheus-rules.yaml
# Collector / Tempo / Loki: use your chart's config value, e.g.
#   --set-file config=deploy/observability/otel-collector.yaml
```

The Tempo/Loki/collector files are **config references**, not Kubernetes manifests and
not HA deployments: they use local disk and single binaries. For production, use the
upstream `tempo-distributed`/`loki` Helm charts with object storage, and deploy the
collector as a Deployment (or DaemonSet) named `otel-collector` in `monitoring`. Keep the
`tempo.monitoring.svc:4317` exporter endpoint or update the chart's
`OTEL_EXPORTER_OTLP_ENDPOINT` to match.

Sampling is decided in the app (`OTEL_TRACES_SAMPLER_ARG`), not in the collector, so
tail sampling cannot re-admit spans that were never exported. Keep probe/scrape paths
out of tracing with `OTEL_IGNORE_PATHS`.

## Alerts

```bash
kubectl apply -n monitoring -f deploy/observability/prometheus-rules.yaml
```

The rules implement the documented SLOs: 99.9% availability (multi-window 14.4× /
6× error-budget burn), complete-answer p95 ≤ 8s, retrieval p95 ≤ 400ms, LLM error
ratio, job backlog and an informational "no chat traffic" signal.

Grafana dashboards, the Loki/Tempo/collector bundle and the on-call runbooks are tracked in
the development tracker; the metric names and span names above are the stable contract they
will be built against.