"""Synthetic probe for a deployed Blackbox instance.

Suitable for a Kubernetes CronJob or an external uptime checker. It exercises the
three things an on-call engineer cares about: the process is alive, the conversation
store is reachable, and (optionally) a full chat turn completes.

    python scripts/smoke_probe.py --base-url https://blackbox.example.com \
        --token "$TOKEN" --chat

Exit code is 1 on any failure, so a CronJob can alert on it.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request


def _request(base_url: str, path: str, token: str | None, timeout: float) -> tuple[int, str]:
    url = base_url.rstrip("/") + path
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001 - probe reports, does not crash
        return 0, type(exc).__name__


def probe(base_url: str, token: str | None, timeout: float, chat: bool) -> tuple[bool, list[str]]:
    failures: list[str] = []

    status, _ = _request(base_url, "/health", token, timeout)
    if status != 200:
        failures.append(f"/health returned {status}")

    status, body = _request(base_url, "/health/ready", token, timeout)
    if status != 200:
        failures.append(f"/health/ready returned {status}: {body[:120]}")

    status, body = _request(base_url, "/metrics", token, timeout)
    if status != 200 or "blackbox_build_info" not in body:
        failures.append(f"/metrics unusable (status {status})")

    if chat:
        payload = json.dumps({"message": "health check"}).encode("utf-8")
        url = base_url.rstrip("/") + "/api/chat"
        request = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                **({"Authorization": f"Bearer {token}"} if token else {}),
            },
        )
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
            if "event: final" not in body:
                failures.append("chat turn did not reach a final event")
            elif time.perf_counter() - started > timeout:
                failures.append("chat turn exceeded the timeout")
        except Exception as exc:  # noqa: BLE001 - report
            failures.append(f"chat turn failed: {type(exc).__name__}")

    return not failures, failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--token", default=None)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--chat", action="store_true")
    args = parser.parse_args()

    ok, failures = probe(args.base_url, args.token, args.timeout, args.chat)
    if ok:
        print("probe: OK")
        return 0
    for failure in failures:
        print(f"probe: FAIL {failure}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())