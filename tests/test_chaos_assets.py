"""Chaos Mesh manifests are reference experiments; keep them parseable and scoped."""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1] / "deploy" / "chaos"


def test_chaos_manifests_are_valid_and_scoped():
    manifests = sorted(ROOT.glob("*.yaml"))
    assert len(manifests) >= 4
    for path in manifests:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert document["apiVersion"].startswith("chaos-mesh.org/")
        assert document["metadata"]["namespace"] == "blackbox"
        assert document["spec"]["duration"]


def test_chaos_manifests_cover_pod_and_network_failures():
    kinds = {
        yaml.safe_load(path.read_text(encoding="utf-8"))["kind"]
        for path in ROOT.glob("*.yaml")
    }
    assert "PodChaos" in kinds
    assert "NetworkChaos" in kinds


def test_chaos_readme_documents_success_criteria():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "BlackboxApiDown" in readme
    assert "8s" in readme
    assert "kubectl delete" in readme