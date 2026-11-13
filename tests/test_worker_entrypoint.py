"""The worker entry point must be able to execute the handlers the API submits."""
import subprocess
import sys


def test_worker_import_registers_every_job_kind():
    code = (
        "import blackbox.worker\n"
        "from blackbox.api.jobs import JOB_KINDS, handler_for\n"
        "missing = [kind for kind in JOB_KINDS if handler_for(kind) is None]\n"
        "raise SystemExit(1 if missing else 0)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
