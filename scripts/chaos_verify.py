"""Check the documented behaviour while a chaos experiment is active.

Usage:
    python scripts/chaos_verify.py --base-url http://localhost:8000

Standard-library only, so it runs from any operator workstation or a CI job. It
asserts the criteria in `deploy/chaos/README.md`: the process is healthy, readiness
reports its dependencies, `/metrics` is served, and one chat turn completes within
the answer SLO or returns the honest-failure path.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request

ANSWER_SLO_S = 8.0


def _get(url: str, timeout: float = 5.0) -> tuple[int, bytes]:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _post_json(url: str, payload: dict, timeout: float = 30.0) -> tuple[int, bytes]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, method="POST", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def run(base_url: str) -> dict:
    base = base_url.rstrip("/")
    checks: dict[str, bool] = {}

    status, _ = _get(f"{base}/health")
    checks["health"] = status == 200

    status, body = _get(f"{base}/health/ready")
    checks["ready"] = status in (200, 503)  # 503 is a documented degraded state
    checks["ready_200"] = status == 200

    status, body = _get(f"{base}/metrics")
    checks["metrics"] = status == 200 and b"blackbox_build_info" in body

    started = time.perf_counter()
    status, raw = _post_json(
        f"{base}/api/chat",
        {"message": "checkout p99 is spiking, has this happened before?"},
    )
    elapsed = time.perf_counter() - started
    answered = status == 200
    checks["chat_served"] = answered
    checks["chat_within_slo"] = answered and elapsed <= ANSWER_SLO_S

    # A 200 SSE body is either a grounded answer or the honest-failure path; both are
    # acceptable under chaos. A 5xx is not.
    checks["no_5xx"] = status < 500
    text = raw.decode("utf-8", errors="ignore")
    checks["honest_or_grounded"] = answered and (
        "insufficient_evidence" in text
        or "event: final" in text
        or "answer" in text
    )

    passed = all(checks.values())
    return {
        "base_url": base,
        "elapsed_s": round(elapsed, 3),
        "checks": checks,
        "passed": passed,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    args = parser.parse_args(argv)

    result = run(args.base_url)
    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
