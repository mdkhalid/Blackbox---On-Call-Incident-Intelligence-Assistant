"""Restore-from-backup drill with measured RTO/RPO.

Usage:
    python scripts/restore_drill.py --write

Seeds a source database, takes a backup, restores it into a fresh database, and
measures how long the restore takes and how many rows it lost.
"""
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", default="data/restore-drill")
    parser.add_argument("--tenant", default="default")
    parser.add_argument("--conversations", type=int, default=10)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)

    from blackbox.api.store import SqlAlchemyConversationRepository
    from blackbox.ops.drills import make_backup, measure_restore

    work = Path(args.work_dir)
    work.mkdir(parents=True, exist_ok=True)
    for name in ("source.db", "backup.db", "restored.db"):
        (work / name).unlink(missing_ok=True)

    source_url = f"sqlite:///{(work / 'source.db').as_posix()}"
    repo = SqlAlchemyConversationRepository(source_url)
    repo.init()
    for index in range(args.conversations):
        conversation_id = f"restore-{index}"
        repo.get_or_create(conversation_id, tenant_id=args.tenant)
        repo.append_turn(
            conversation_id, "q", "a", "answered", [], {}, tenant_id=args.tenant
        )

    backup = work / "backup.db"
    make_backup(source_url, backup)
    result = measure_restore(
        source_url=source_url,
        backup_path=backup,
        restored_url=f"sqlite:///{(work / 'restored.db').as_posix()}",
        tenant_id=args.tenant,
    )

    payload = {
        "run_at": datetime.now(UTC).isoformat(),
        "result": result.to_dict(),
    }
    if args.write:
        out = Path("docs/dr-drill-results.md")
        out.parent.mkdir(parents=True, exist_ok=True)
        d = result.to_dict()
        section = (
            "\n## Restore drill\n\n"
            f"- Run at: {payload['run_at']}\n"
            f"- RTO: {d['rto_s']} s\n"
            f"- RPO: {d['rpo_rows']} rows\n"
            f"- Rows restored: {d['rows_after']} / {d['rows_before']}\n"
        )
        with out.open("a", encoding="utf-8") as handle:
            handle.write(section)
        payload["written_to"] = str(out)
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
