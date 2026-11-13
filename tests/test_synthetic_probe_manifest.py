"""Synthetic probe manifest tests."""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1] / "deploy" / "probes"


def test_synthetic_probe_cronjob_is_valid_and_scoped():
    document = yaml.safe_load((ROOT / "synthetic-probe-cronjob.yaml").read_text(encoding="utf-8"))
    assert document["kind"] == "CronJob"
    assert document["metadata"]["namespace"] == "blackbox"
    assert document["spec"]["schedule"] == "*/5 * * * *"
    assert document["spec"]["jobTemplate"]["spec"]["template"]["spec"]["restartPolicy"] == "Never"