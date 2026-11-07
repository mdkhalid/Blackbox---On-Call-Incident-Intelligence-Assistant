"""Deterministic corpus generator for Blackbox.

Run: python seed/generate_corpus.py [--out seed/corpus]
Produces 30 postmortems, 15 runbooks, 10 arch docs, 5 alert defs.
"""
from __future__ import annotations

import argparse
import random
import textwrap
from pathlib import Path

SEED = 42
SERVICES = [
    "checkout-service",
    "payment-gateway",
    "inventory-service",
    "order-service",
    "user-service",
    "notification-service",
    "search-service",
    "auth-service",
]
ERROR_CODES = [
    "ERR_POOL_EXHAUSTED",
    "ERR_TIMEOUT",
    "ERR_CONN_REFUSED",
    "ERR_RATE_LIMIT",
    "ERR_DB_DEADLOCK",
    "ERR_CACHE_MISS",
    "ERR_AUTH_FAILED",
]
DEPENDENCIES = {
    "checkout-service": ["payment-gateway", "inventory-service"],
    "order-service": ["inventory-service", "payment-gateway"],
    "search-service": ["inventory-service"],
    "notification-service": ["user-service"],
    "auth-service": ["user-service"],
}

SEVERITIES = ["SEV1", "SEV2", "SEV3"]


def fm(meta: dict, body: str) -> str:
    import yaml

    front = yaml.safe_dump(meta, sort_keys=False).strip()
    return f"---\n{front}\n---\n\n{body.strip()}\n"


def gen_postmortems(out: Path, rng: random.Random):
    out.mkdir(parents=True, exist_ok=True)
    for i in range(1, 31):
        svc = rng.choice(SERVICES)
        deps = DEPENDENCIES.get(svc, [])
        sev = rng.choice(SEVERITIES)
        code = rng.choice(ERROR_CODES)
        date = f"2024-{rng.randint(1,12):02d}-{rng.randint(1,28):02d}"
        title = f"{svc} {code} incident"
        meta = {
            "id": f"pm-{i:03d}",
            "title": title,
            "doc_type": "postmortem",
            "services": [svc] + (deps[:1] if deps else []),
            "severity": sev,
            "date": date,
            "error_codes": [code],
        }
        body = textwrap.dedent(
            f"""\
            ## Summary
            On {date}, {svc} experienced elevated latency and errors due to {code}. Customers reported checkout failures.

            ## Impact
            Approximately {rng.randint(1,40)}% of requests failed for {rng.randint(5,60)} minutes. Revenue impact estimated at ${rng.randint(1,50)}k.

            ## Timeline
            - {date} 01:10 - First alert fired for {svc}.
            - {date} 01:15 - On-call paged.
            - {date} 01:22 - {code} observed in logs.
            - {date} 01:35 - Mitigation applied: restarted connection pool.
            - {date} 01:50 - Service recovered.

            ## Root Cause
            The root cause was {code} in {svc}. The connection pool became saturated because upstream {deps[0] if deps else 'dependency'} slowed down, causing requests to pile up and everything to slow down. No code change to pool sizing; load exceeded configured limits.

            ## Contributing Factors
            - Missing autoscaling for {svc}.
            - Alert threshold too high.
            - Runbook not linked from alert.

            ## Remediation
            Increased pool size and added circuit breaker. Deployed hotfix to {svc}.

            ## Action Items
            - [ ] Add load test for {svc}.
            - [ ] Review alert thresholds.
            - [ ] Update runbook RB-{rng.randint(1,15):03d}.
            """
        )
        (out / f"pm-{i:03d}.md").write_text(fm(meta, body), encoding="utf-8")


def gen_runbooks(out: Path, rng: random.Random):
    out.mkdir(parents=True, exist_ok=True)
    for i in range(1, 16):
        svc = rng.choice(SERVICES)
        code = rng.choice(ERROR_CODES)
        meta = {
            "id": f"rb-{i:03d}",
            "title": f"Runbook: {svc} {code}",
            "doc_type": "runbook",
            "services": [svc],
            "severity": "SEV2",
            "date": "2024-01-01",
            "error_codes": [code],
        }
        body = textwrap.dedent(
            f"""\
            ## Symptom
            {svc} reports {code}; p99 latency spiking; requests pile up.

            ## Diagnosis
            1. Check connection pool metrics for {svc}.
            2. Verify upstream dependencies: {", ".join(DEPENDENCIES.get(svc, ["none"]))}.
            3. Look for {code} in logs.
            4. Confirm no recent deploy at the time of incident.

            ## Mitigation
            - Restart {svc} pods.
            - Scale up connection pool.
            - Enable circuit breaker to upstream.

            ## Escalation
            Escalate to platform team if not resolved in 30 minutes. Page service owner.
            """
        )
        (out / f"rb-{i:03d}.md").write_text(fm(meta, body), encoding="utf-8")


def gen_arch(out: Path, rng: random.Random):
    out.mkdir(parents=True, exist_ok=True)
    for i, svc in enumerate(SERVICES, start=1):
        # ensure 10 docs: repeat/extend
        pass
    # generate 10 arch docs (cycle services)
    for i in range(1, 11):
        svc = SERVICES[(i - 1) % len(SERVICES)]
        deps = DEPENDENCIES.get(svc, [])
        meta = {
            "id": f"arch-{i:03d}",
            "title": f"Architecture: {svc}",
            "doc_type": "arch",
            "services": [svc] + deps,
            "severity": "SEV3",
            "date": "2024-01-01",
            "error_codes": [],
        }
        body = textwrap.dedent(
            f"""\
            ## Service Description
            {svc} handles core business logic and depends on upstream services.

            ## Dependencies
            {svc} depends on: {", ".join(deps) if deps else "none"}.

            ## Data Flows
            Requests flow from API gateway to {svc}, then to {", ".join(deps) if deps else "data store"}.

            ## Known Limits
            Connection pool size 50. Max RPS 1000. Latency SLO p99 < 200ms.
            """
        )
        (out / f"arch-{i:03d}.md").write_text(fm(meta, body), encoding="utf-8")


def gen_alerts(out: Path, rng: random.Random):
    out.mkdir(parents=True, exist_ok=True)
    for i in range(1, 6):
        svc = rng.choice(SERVICES)
        code = rng.choice(ERROR_CODES)
        meta = {
            "id": f"alert-{i:03d}",
            "title": f"Alert: {svc} {code}",
            "doc_type": "alert",
            "services": [svc],
            "severity": "SEV2",
            "date": "2024-01-01",
            "error_codes": [code],
        }
        body = textwrap.dedent(
            f"""\
            ## Alert Definition
            Alert name: {svc}_{code}_high
            Condition: error rate > 5% for 5 minutes
            Threshold: 5%
            Linked runbook: RB-{rng.randint(1,15):03d}
            """
        )
        (out / f"alert-{i:03d}.md").write_text(fm(meta, body), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="seed/corpus")
    args = parser.parse_args()
    rng = random.Random(SEED)
    out = Path(args.out)
    gen_postmortems(out / "postmortems", rng)
    gen_runbooks(out / "runbooks", rng)
    gen_arch(out / "arch", rng)
    gen_alerts(out / "alerts", rng)
    # counts
    total = sum(1 for _ in out.rglob("*.md"))
    print(f"Generated {total} documents under {out}")


if __name__ == "__main__":
    main()
