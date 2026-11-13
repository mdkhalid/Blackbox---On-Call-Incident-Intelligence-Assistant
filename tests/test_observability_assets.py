"""The dashboards and collector configs are contracts too.

A dashboard that queries a metric the app no longer exports is worse than no dashboard:
it silently renders an empty panel. These tests keep the Prometheus/Tempo/Loki assets
aligned with the registry and with each other.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from blackbox.observability.metrics import render_metrics

ROOT = Path(__file__).resolve().parents[1] / "deploy" / "observability"
DASHBOARD = ROOT / "grafana" / "dashboards" / "blackbox-overview.json"
DATASOURCES = ROOT / "grafana" / "provisioning" / "datasources" / "datasources.yaml"
DASHBOARD_PROVIDER = ROOT / "grafana" / "provisioning" / "dashboards" / "dashboards.yaml"
_METRIC = re.compile(r"\bblackbox_[a-z0-9_]+")
_HISTOGRAM_SUFFIXES = ("_bucket", "_sum", "_count")


def _dashboard() -> dict:
    return json.loads(DASHBOARD.read_text(encoding="utf-8"))


def _panels() -> list[dict]:
    return [panel for panel in _dashboard()["panels"] if panel["type"] != "row"]


def _metric_base(name: str) -> str:
    for suffix in _HISTOGRAM_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def test_dashboard_is_valid_and_has_the_expected_rows():
    dashboard = _dashboard()
    assert dashboard["uid"] == "blackbox-overview"
    assert dashboard["title"] == "Blackbox overview"
    assert dashboard["refresh"]
    rows = {panel["title"] for panel in dashboard["panels"] if panel["type"] == "row"}
    assert {"Service health", "Traffic and latency", "Retrieval and LLM", "Traces and logs"} <= rows
    assert len(_panels()) >= 12


def test_every_blackbox_metric_in_the_dashboard_is_exported():
    rendered = render_metrics()
    referenced: set[str] = set()
    for panel in _panels():
        for target in panel.get("targets", []):
            expr = target.get("expr", "")
            referenced.update(_METRIC.findall(expr))

    missing = {name for name in referenced if _metric_base(name) not in rendered}
    assert not missing, f"dashboard references unknown metrics: {sorted(missing)}"


def test_dashboard_targets_use_declared_datasources():
    provisioned = yaml.safe_load(DATASOURCES.read_text(encoding="utf-8"))["datasources"]
    uids = {source["uid"] for source in provisioned}
    for panel in _panels():
        for target in panel.get("targets", []):
            uid = target["datasource"]["uid"]
            assert uid in uids, f"panel {panel['title']!r} uses undeclared datasource {uid!r}"


def test_dashboard_queries_never_label_on_a_raw_path_or_conversation():
    for panel in _panels():
        for target in panel.get("targets", []):
            expr = target.get("expr", "")
            assert "conversation" not in expr.lower()
            assert not re.search(r"path\s*=~\s*\"?/api/conversations", expr)
            assert "path=~" not in expr


def test_grafana_provisioning_links_traces_and_logs():
    provisioned = yaml.safe_load(DATASOURCES.read_text(encoding="utf-8"))["datasources"]
    by_uid = {source["uid"]: source for source in provisioned}

    assert by_uid["prometheus"]["type"] == "prometheus"
    assert by_uid["loki"]["type"] == "loki"
    assert by_uid["tempo"]["type"] == "tempo"
    derived = by_uid["loki"]["jsonData"]["derivedFields"][0]
    assert derived["datasourceUid"] == "tempo"
    assert "trace_id" in derived["matcherRegex"]
    traces_to_logs = by_uid["tempo"]["jsonData"]["tracesToLogsV2"]
    assert traces_to_logs["datasourceUid"] == "loki"
    assert traces_to_logs["filterByTraceID"] is True

    provider = yaml.safe_load(DASHBOARD_PROVIDER.read_text(encoding="utf-8"))
    paths = [item["options"]["path"] for item in provider["providers"]]
    assert paths == ["/var/lib/grafana/dashboards"]


def test_collector_pipeline_exports_to_tempo():
    config = yaml.safe_load((ROOT / "otel-collector.yaml").read_text(encoding="utf-8"))
    protocols = config["receivers"]["otlp"]["protocols"]
    assert "grpc" in protocols and "http" in protocols
    pipeline = config["service"]["pipelines"]["traces"]
    assert pipeline["receivers"] == ["otlp"]
    # Batching and memory protection belong at the collector, not only in the app.
    assert "batch" in pipeline["processors"]
    assert "memory_limiter" in pipeline["processors"]
    exporter = config["exporters"]["otlp/tempo"]
    assert exporter["endpoint"] == "tempo.monitoring.svc:4317"
    assert exporter["sending_queue"]["enabled"] is True


def test_tempo_and_loki_configs_are_runnable_references():
    tempo = yaml.safe_load((ROOT / "tempo.yaml").read_text(encoding="utf-8"))
    assert tempo["server"]["http_listen_port"] == 3200
    assert tempo["distributor"]["receivers"]["otlp"]["protocols"]["grpc"]["endpoint"].endswith(":4317")
    assert tempo["storage"]["trace"]["backend"] == "local"
    assert tempo["compactor"]["compaction"]["block_retention"]

    loki = yaml.safe_load((ROOT / "loki.yaml").read_text(encoding="utf-8"))
    assert loki["server"]["http_listen_port"] == 3100
    assert loki["schema_config"]["configs"][0]["schema"] == "v13"
    assert loki["limits_config"]["retention_period"] == "168h"


def test_promtail_ships_json_logs_and_extracts_trace_ids():
    values = yaml.safe_load((ROOT / "promtail-values.yaml").read_text(encoding="utf-8"))
    clients = values["config"]["clients"]
    assert clients[0]["url"].endswith("/loki/api/v1/push")

    stages = values["config"]["snippets"]["pipelineStages"]
    json_stage = next(stage for stage in stages if "json" in stage)
    assert "trace_id" in json_stage["json"]["expressions"]
    labels = next(stage for stage in stages if "labels" in stage)
    assert "trace_id" in labels["labels"]


def test_observability_readme_documents_the_stack():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for name in (
        "blackbox-overview.json",
        "otel-collector.yaml",
        "tempo.yaml",
        "loki.yaml",
        "promtail-values.yaml",
        "datasources.yaml",
    ):
        assert name in readme
