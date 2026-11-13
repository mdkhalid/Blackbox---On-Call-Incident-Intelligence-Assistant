"""Synthetic probe tests."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "smoke_probe.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("smoke_probe", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["smoke_probe"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_probe_reports_failures_for_unreachable_host():
    module = _load_module()
    ok, failures = module.probe("http://127.0.0.1:1", None, 1.0, False)
    assert ok is False
    assert failures


def test_cli_help_exits_zero():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "--base-url" in result.stdout