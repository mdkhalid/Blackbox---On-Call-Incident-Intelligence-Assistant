"""GitOps assets: Argo CD application, canary rollout, analysis and canary service.

These are reference manifests, not core Kubernetes objects, so the CI kubeconform run
does not validate them. The tests here keep their structure aligned with the chart and
with the metric names the analysis queries.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1] / "deploy" / "gitops"
_METRIC = re.compile(r"\bblackbox_[a-z0-9_]+")
_HISTOGRAM_SUFFIXES = ("_bucket", "_sum", "_count")


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_argocd_application_points_at_the_chart_with_api_disabled():
    app = _load(ROOT / "application-chart.yaml")
    assert app["kind"] == "Application"
    assert app["spec"]["source"]["path"] == "deploy/helm/blackbox"
    value_files = app["spec"]["source"]["helm"]["valueFiles"]
    assert "../../gitops/argocd-values.yaml" in value_files

    values = _load(ROOT / "argocd-values.yaml")
    assert values["api"]["enabled"] is False
    assert values["api"]["autoscaling"]["enabled"] is False
    assert values["migration"]["enabled"] is True
    assert values["networkPolicy"]["enabled"] is True


def test_canary_rollout_has_increasing_weights_and_analysis():
    rollout = _load(ROOT / "api-rollout.yaml")
    assert rollout["kind"] == "Rollout"
    assert rollout["spec"]["strategy"]["canary"]["stableService"] == "blackbox"
    assert rollout["spec"]["strategy"]["canary"]["canaryService"] == "blackbox-canary"

    steps = rollout["spec"]["strategy"]["canary"]["steps"]
    weights = [step["setWeight"] for step in steps if "setWeight" in step]
    assert weights == sorted(set(weights)) and weights[-1] == 100
    assert any("analysis" in step for step in steps)
    assert steps[-1]["setWeight"] == 100

    template = rollout["spec"]["template"]
    assert template["spec"]["serviceAccountName"] == "blackbox"
    assert "startupProbe" in template["spec"]["containers"][0]
    assert "readinessProbe" in template["spec"]["containers"][0]


def test_analysis_template_queries_known_metrics():
    template = _load(ROOT / "analysis-template.yaml")
    assert template["kind"] == "AnalysisTemplate"
    metrics = template["spec"]["metrics"]
    assert len(metrics) >= 3
    for metric in metrics:
        assert metric["provider"]["prometheus"]["address"].startswith("http://")
        assert metric["successCondition"]
        query = metric["provider"]["prometheus"]["query"]
        assert _METRIC.search(query) or "blackbox:" in query


def test_canary_service_matches_the_chart_service_shape():
    service = _load(ROOT / "canary-service.yaml")
    assert service["kind"] == "Service"
    assert service["metadata"]["name"] == "blackbox-canary"
    ports = service["spec"]["ports"]
    assert any(port["name"] == "http" and port["port"] == 80 for port in ports)


def test_gitops_readme_documents_ownership_and_rollback():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "api.enabled: false" in readme
    assert "kubectl argo rollouts" in readme
    assert "undo" in readme
    assert "migration" in readme.lower()