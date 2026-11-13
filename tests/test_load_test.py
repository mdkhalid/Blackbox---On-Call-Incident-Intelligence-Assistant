"""Tests for the dependency-free load harness (scripts/load_test.py).

The harness never calls the network in these tests; they exercise the pure helpers
so a change to the percentile math or summary shape is caught without a server.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "load_test.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("load_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["load_test"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_percentile_handles_empty_and_extremes():
    module = _load_module()
    assert module.percentile([], 50) == 0.0
    assert module.percentile([1.0, 2.0, 3.0, 4.0], 0) == 1.0
    assert module.percentile([1.0, 2.0, 3.0, 4.0], 100) == 4.0
    assert module.percentile([1.0, 2.0, 3.0, 4.0], 50) == pytest.approx(2.5)


def test_percentile_interpolates():
    module = _load_module()
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert module.percentile(values, 90) == pytest.approx(46.0)


def test_summarise_reports_errors_and_throughput():
    module = _load_module()
    samples = [
        module.Sample(ok=True, http_status=200, ttft_s=0.1, total_s=1.0, answer_chars=42),
        module.Sample(ok=True, http_status=200, ttft_s=0.2, total_s=2.0, answer_chars=40),
        module.Sample(ok=False, http_status=503, ttft_s=None, total_s=0.5, answer_chars=0, error="http_503"),
    ]
    summary = module._summarise(samples, wall_s=2.0)
    assert summary["requests"] == 3
    assert summary["success"] == 2
    assert summary["errors"] == 1
    assert summary["error_rate"] == pytest.approx(1 / 3, abs=1e-4)
    assert summary["error_breakdown"] == {"http_503": 1}
    assert summary["throughput_rps"] == pytest.approx(1.5)
    assert summary["turn_seconds"]["p95"] > 0


def test_cli_help_exits_zero():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "--base-url" in result.stdout
    assert "--concurrency" in result.stdout