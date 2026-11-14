"""Rebuild a region's retrieval indexes from a document corpus.

Usage:
    python scripts/rebuild_region_indexes.py --source seed/corpus --validate

Reads `SETTINGS` from the environment (REGION, DATABASE_URL, QDRANT_URL,
OPENSEARCH_HOSTS, ...) plus `blackbox/config/config.yaml`. With `--validate` the
rebuild runs through the staged path (when an OpenSearch alias is configured) and
fails if the post-swap validation does not pass.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="seed/corpus", help="corpus root directory")
    parser.add_argument("--tenant", default="default", help="tenant id to attribute the rebuild to")
    parser.add_argument(
        "--validate", action="store_true", help="validate the rebuilt index before serving"
    )
    args = parser.parse_args(argv)

    from blackbox.config.settings import get_config, get_settings
    from blackbox.ingestion.rebuild import rebuild_region_indexes

    settings = get_settings()
    config = get_config()
    source = Path(args.source)
    if not source.exists():
        print(f"source directory not found: {source}", file=sys.stderr)
        return 2

    try:
        result = rebuild_region_indexes(
            settings,
            config,
            source_dir=source,
            tenant_id=args.tenant,
            force=True,
        )
    except Exception as exc:  # noqa: BLE001 - a failed rebuild must surface as a non-zero exit
        print(f"rebuild failed: {exc}", file=sys.stderr)
        return 1

    if args.validate and not result.get("validation", {}).get("passed", True):
        print("rebuild validation failed", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
