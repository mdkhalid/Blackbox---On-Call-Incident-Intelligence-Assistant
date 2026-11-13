"""The alert rules are a contract with the metrics the app actually exports."""
import re
from pathlib import Path

import yaml

from blackbox.observability.metrics import render_metrics

RULES = Path(__file__).resolve().parents[1] / "deploy" / "observability" / "prometheus-rules.yaml"
_METRIC = re.compile(r"\bblackbox_[a-z0-9_]+")
_HISTOGRAM_SUFFIXES = ("_bucket", "_sum", "_count")


def _rules() -> list[dict]:
    document = yaml.safe_load(RULES.read_text(encoding="utf-8"))
    return [rule for group in document["groups"] for rule in group["rules"]]


def test_alert_rules_parse_and_are_unique():
    alerts = [rule for rule in _rules() if "alert" in rule]
    names = [alert["alert"] for alert in alerts]
    assert len(names) >= 8
    assert len(names) == len(set(names))
    for alert in alerts:
        assert alert["expr"].strip()
        assert alert["labels"]["severity"] in {"critical", "warning", "info"}
        assert alert["annotations"]["summary"]


def test_every_referenced_metric_is_exported_by_the_registry():
    rendered = render_metrics()
    referenced: set[str] = set()
    for rule in _rules():
        referenced.update(_METRIC.findall(rule["expr"]))
        for metric in _METRIC.findall(rule.get("record", "")):
            referenced.add(metric)

    # Recording-rule names use `blackbox:` rather than `blackbox_` and are not
    # registry metrics; histogram suffixes are derived from the base family.
    missing = set()
    for name in referenced:
        base = name
        for suffix in _HISTOGRAM_SUFFIXES:
            if name.endswith(suffix):
                base = name[: -len(suffix)]
                break
        if base not in rendered:
            missing.add(name)
    assert not missing, f"alert rules reference unknown metrics: {sorted(missing)}"
