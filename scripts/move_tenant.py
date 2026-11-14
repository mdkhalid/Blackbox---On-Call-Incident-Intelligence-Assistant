"""Move a tenant's conversations to another region's database.

Usage:
    python scripts/move_tenant.py --tenant acme --target-region us-east-1 \
        --source-url sqlite:///data/eu.db --target-url sqlite:///data/us.db

Import first, verify, then flip the home-region mapping. A failure before the flip
leaves the tenant served from its old home region, which is the safe state.
"""
from __future__ import annotations

import argparse
import json


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--target-region", required=True)
    parser.add_argument("--source-url", default="sqlite:///data/move_source.db")
    parser.add_argument("--target-url", default="sqlite:///data/move_target.db")
    args = parser.parse_args(argv)

    from blackbox.ops.drills import move_tenant

    try:
        result = move_tenant(
            source_url=args.source_url,
            target_url=args.target_url,
            tenant_id=args.tenant,
            target_region=args.target_region,
        )
    except Exception as exc:  # noqa: BLE001 - a failed move must be a non-zero exit
        print(f"move failed: {exc}", file=__import__("sys").stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0 if result.get("verified") else 1


if __name__ == "__main__":
    raise SystemExit(main())
