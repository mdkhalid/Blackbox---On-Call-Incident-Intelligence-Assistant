"""Dependency-free load harness for the Blackbox SSE chat endpoint.

The service is meant to hold 2k concurrent SSE streams. This script measures the
two numbers that matter for that: time to first token (TTFT) and end-to-end turn
latency, plus the error rate and throughput. It uses only the standard library so
it can run from a laptop or a CI runner without installing a load-testing tool.

Usage:

    python scripts/load_test.py --base-url http://localhost:8000 \
        --concurrency 50 --requests 200 --token "$TOKEN"

Exit code is 1 when the error rate exceeds 1% or the p95 turn latency exceeds the
configured SLO (8s by default), so it can gate a release.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class Sample:
    ok: bool
    http_status: int
    ttft_s: float | None
    total_s: float
    answer_chars: int
    error: str | None = None


def percentile(values: list[float], p: float) -> float:
    """Linear-interpolation percentile; returns 0.0 for an empty input."""
    if not values:
        return 0.0
    if p <= 0:
        return min(values)
    if p >= 100:
        return max(values)
    ordered = sorted(values)
    rank = (p / 100.0) * (len(ordered) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = rank - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _one_request(
    base_url: str,
    message: str,
    conversation_id: str | None,
    token: str | None,
    timeout_s: float,
) -> Sample:
    url = base_url.rstrip("/") + "/api/chat"
    payload = {"message": message}
    if conversation_id:
        payload["conversation_id"] = conversation_id
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    started = time.perf_counter()
    first_token: float | None = None
    answer_chars = 0
    http_status = 0
    error: str | None = None
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            http_status = response.status
            # urlopen returns a buffered reader; readline() yields one SSE line at
            # a time so we can measure TTFT without waiting for the whole body.
            event_name: str | None = None
            for raw in response:
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if line.startswith("event:"):
                    event_name = line[len("event:") :].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                if event_name == "token":
                    if first_token is None:
                        first_token = time.perf_counter()
                    try:
                        data = json.loads(line[len("data:") :].strip())
                        answer_chars += len(data.get("text", ""))
                    except (json.JSONDecodeError, AttributeError):
                        pass
                elif event_name == "error":
                    error = line
    except urllib.error.HTTPError as exc:
        http_status = exc.code
        error = f"http_{exc.code}"
    except Exception as exc:  # noqa: BLE001 - report, do not abort the whole run
        error = type(exc).__name__

    total = time.perf_counter() - started
    ttft = (first_token - started) if first_token is not None else None
    ok = error is None and http_status == 200 and answer_chars > 0
    if not ok and error is None:
        error = "no_answer" if http_status == 200 else f"http_{http_status}"
    return Sample(
        ok=ok,
        http_status=http_status,
        ttft_s=ttft,
        total_s=total,
        answer_chars=answer_chars,
        error=error,
    )


async def _run(
    base_url: str,
    message: str,
    conversation_factory: Callable[[], str | None],
    token: str | None,
    concurrency: int,
    requests: int,
    timeout_s: float,
) -> list[Sample]:
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def bounded() -> Sample:
        async with semaphore:
            return await asyncio.to_thread(
                _one_request,
                base_url,
                message,
                conversation_factory(),
                token,
                timeout_s,
            )

    return await asyncio.gather(*(bounded() for _ in range(requests)))


def _summarise(samples: list[Sample], wall_s: float) -> dict:
    successes = [s for s in samples if s.ok]
    ttfts = [s.ttft_s for s in successes if s.ttft_s is not None]
    totals = [s.total_s for s in successes]
    errors: dict[str, int] = {}
    for sample in samples:
        if not sample.ok:
            errors[sample.error or "unknown"] = errors.get(sample.error or "unknown", 0) + 1

    return {
        "requests": len(samples),
        "concurrency": None,
        "wall_seconds": round(wall_s, 3),
        "throughput_rps": round(len(samples) / wall_s, 3) if wall_s else 0.0,
        "success": len(successes),
        "errors": len(samples) - len(successes),
        "error_rate": round((len(samples) - len(successes)) / len(samples), 4) if samples else 0.0,
        "error_breakdown": errors,
        "ttft_seconds": {
            "p50": round(percentile(ttfts, 50), 4),
            "p95": round(percentile(ttfts, 95), 4),
            "p99": round(percentile(ttfts, 99), 4),
            "max": round(max(ttfts), 4) if ttfts else 0.0,
        },
        "turn_seconds": {
            "p50": round(percentile(totals, 50), 4),
            "p95": round(percentile(totals, 95), 4),
            "p99": round(percentile(totals, 99), 4),
            "max": round(max(totals), 4) if totals else 0.0,
        },
        "answer_chars_avg": round(
            statistics.fmean(s.answer_chars for s in successes), 1
        ) if successes else 0.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--requests", type=int, default=50)
    parser.add_argument("--message", default="What caused ERR_POOL_EXHAUSTED in checkout-service?")
    parser.add_argument("--token", default=None)
    parser.add_argument("--shared-conversation", action="store_true",
                        help="reuse one conversation id (exercises the per-conversation lock)")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--slo-p95-seconds", type=float, default=8.0)
    parser.add_argument("--max-error-rate", type=float, default=0.01)
    parser.add_argument("--output", choices=("text", "json"), default="text")
    args = parser.parse_args()

    if args.requests < 1 or args.concurrency < 1:
        parser.error("--requests and --concurrency must be >= 1")

    shared_id = str(uuid.uuid4()) if args.shared_conversation else None

    def conversation_factory() -> str | None:
        return shared_id if shared_id else str(uuid.uuid4())

    wall_started = time.perf_counter()
    samples = asyncio.run(
        _run(
            base_url=args.base_url,
            message=args.message,
            conversation_factory=conversation_factory,
            token=args.token,
            concurrency=args.concurrency,
            requests=args.requests,
            timeout_s=args.timeout,
        )
    )
    wall = time.perf_counter() - wall_started
    summary = _summarise(samples, wall)
    summary["concurrency"] = args.concurrency

    if args.output == "json":
        print(json.dumps(summary, indent=2))
    else:
        print(f"requests={summary['requests']} concurrency={summary['concurrency']} "
              f"wall={summary['wall_seconds']}s throughput={summary['throughput_rps']} rps")
        print(f"success={summary['success']} errors={summary['errors']} "
              f"error_rate={summary['error_rate']:.2%}")
        print(f"TTFT p50/p95/p99/max = "
              f"{summary['ttft_seconds']['p50']}/{summary['ttft_seconds']['p95']}/"
              f"{summary['ttft_seconds']['p99']}/{summary['ttft_seconds']['max']} s")
        print(f"turn p50/p95/p99/max = "
              f"{summary['turn_seconds']['p50']}/{summary['turn_seconds']['p95']}/"
              f"{summary['turn_seconds']['p99']}/{summary['turn_seconds']['max']} s")
        if summary["error_breakdown"]:
            print(f"error_breakdown={summary['error_breakdown']}")

    failed = False
    if summary["error_rate"] > args.max_error_rate:
        failed = True
    if summary["success"] and summary["turn_seconds"]["p95"] > args.slo_p95_seconds:
        failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())