"""Disaster-recovery drills and the tenant-move procedure.

The functions here are deliberately small and testable: they take database URLs (and an
optional promotion callable), do the thing, and return measured numbers. The scripts in
`scripts/` are thin wrappers that write the results to `docs/dr-drill-results.md`.

For a SQLite drill, "promotion" is a file copy: the harness models the control-plane
steps (stop writes, promote, repoint) so RTO/RPO accounting can be exercised offline.
Against Postgres, pass `promote` to run `pg_ctl promote` or the managed failover call;
the measurement is the same.
"""
from __future__ import annotations

import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from blackbox.api.store import DEFAULT_TENANT, SqlAlchemyConversationRepository
from blackbox.logging_setup import get_logger

logger = get_logger(__name__)


@dataclass
class DrillResult:
    name: str
    topology: str
    rto_s: float
    rpo_rows: int
    rows_before: int
    rows_after: int
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "topology": self.topology,
            "rto_s": round(self.rto_s, 3),
            "rpo_rows": self.rpo_rows,
            "rows_before": self.rows_before,
            "rows_after": self.rows_after,
            "notes": list(self.notes),
        }


def _turn_count(repo: SqlAlchemyConversationRepository, tenant_id: str) -> int:
    return len(repo.turns_for_tenant(tenant_id, limit=100_000))


def _seed(repo: SqlAlchemyConversationRepository, tenant_id: str, conversations: int) -> None:
    for index in range(conversations):
        conversation_id = f"drill-{index}"
        repo.get_or_create(conversation_id, tenant_id=tenant_id)
        repo.append_turn(
            conversation_id,
            "what happened?",
            f"turn {index}",
            "answered",
            [],
            {},
            tenant_id=tenant_id,
        )


def _sqlite_path(url: str) -> Path | None:
    if not url.startswith("sqlite:///"):
        return None
    raw = url[len("sqlite:///") :]
    if not raw or raw in (":memory:", "/:memory:"):
        return None
    return Path(raw)


def simulate_failover(
    *,
    source_url: str,
    target_url: str,
    conversations: int = 20,
    tenant_id: str = DEFAULT_TENANT,
    promote: Callable[[], None] | None = None,
) -> DrillResult:
    """Seed a primary, promote the replica, and measure RTO/RPO.

    RTO is the wall time from "primary stopped" to the first successful read and write
    on the promoted target. RPO is the number of turns the target lost.
    """
    source_path = _sqlite_path(source_url)
    target_path = _sqlite_path(target_url)
    # Fail before touching either database: there is no point seeding a primary for a
    # drill that has no way to promote the replica.
    if (source_path is None or target_path is None) and promote is None:
        raise ValueError(
            "a non-SQLite failover needs a promote callable or a replica endpoint"
        )

    source = SqlAlchemyConversationRepository(source_url)
    source.init()
    _seed(source, tenant_id, conversations)
    rows_before = _turn_count(source, tenant_id)

    started = time.perf_counter()
    if source_path is not None and target_path is not None:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, target_path)
    elif promote is not None:
        promote()

    target = SqlAlchemyConversationRepository(target_url)
    # The first successful read is the service being available again.
    _ = target.turns_for_tenant(tenant_id, limit=1)
    rows_after = _turn_count(target, tenant_id)
    # A canary write proves the promoted target accepts new turns.
    target.get_or_create("drill-post-failover", tenant_id=tenant_id)
    target.append_turn(
        "drill-post-failover", "after failover", "ok", "answered", [], {},
        tenant_id=tenant_id,
    )
    rto = time.perf_counter() - started

    logger.info(
        "dr_drill_failover",
        rto_s=round(rto, 3),
        rpo_rows=max(0, rows_before - rows_after),
    )
    return DrillResult(
        name="failover",
        topology="sqlite file copy" if source_path is not None else "promote callable",
        rto_s=rto,
        rpo_rows=max(0, rows_before - rows_after),
        rows_before=rows_before,
        rows_after=rows_after,
        notes=["RTO measured to first read + canary write on the promoted target"],
    )


def measure_restore(
    *,
    source_url: str,
    backup_path: Path,
    restored_url: str,
    tenant_id: str = DEFAULT_TENANT,
) -> DrillResult:
    """Restore a backup into a fresh database and measure restore RTO/RPO."""
    source = SqlAlchemyConversationRepository(source_url)
    source.init()
    rows_before = _turn_count(source, tenant_id)

    if not backup_path.exists():
        raise FileNotFoundError(f"backup not found: {backup_path}")
    target_path = _sqlite_path(restored_url)
    if target_path is None:
        raise ValueError("restore drill currently models a SQLite backup")

    started = time.perf_counter()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(backup_path, target_path)
    restored = SqlAlchemyConversationRepository(restored_url)
    _ = restored.turns_for_tenant(tenant_id, limit=1)
    rows_after = _turn_count(restored, tenant_id)
    rto = time.perf_counter() - started

    return DrillResult(
        name="restore",
        topology="sqlite backup copy",
        rto_s=rto,
        rpo_rows=max(0, rows_before - rows_after),
        rows_before=rows_before,
        rows_after=rows_after,
        notes=[f"backup: {backup_path.name}"],
    )


def make_backup(source_url: str, backup_path: Path) -> None:
    """Snapshot a SQLite source for the restore drill (outside the timed window)."""
    source_path = _sqlite_path(source_url)
    if source_path is None:
        raise ValueError("only a SQLite source can be backed up by this harness")
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_path, backup_path)


def move_tenant(
    *,
    source_url: str,
    target_url: str,
    tenant_id: str,
    target_region: str,
) -> dict[str, Any]:
    """Move one tenant to another region's database, then flip its home region.

    Order matters: import first, verify, and only then repoint `tenant_regions`. A
    failure before the flip leaves the tenant served from its old home region, which
    is the safe state.
    """
    source = SqlAlchemyConversationRepository(source_url)
    source.init()
    target = SqlAlchemyConversationRepository(target_url)
    target.init()

    exported = source.export_tenant(tenant_id)
    imported = target.import_tenant(exported, home_region=target_region)
    source.set_home_region(tenant_id, target_region, status="moving")

    source_turns = len(exported["turns"])
    target_turns = _turn_count(target, tenant_id)
    verified = target_turns >= source_turns
    if verified:
        target.set_home_region(tenant_id, target_region, status="active")

    result = {
        "tenant_id": tenant_id,
        "target_region": target_region,
        "conversations_exported": len(exported["conversations"]),
        "turns_exported": source_turns,
        "conversations_imported": imported["conversations"],
        "turns_after": target_turns,
        "verified": verified,
        "status": "active" if verified else "moving",
    }
    logger.info("tenant_move", **result)
    return result
