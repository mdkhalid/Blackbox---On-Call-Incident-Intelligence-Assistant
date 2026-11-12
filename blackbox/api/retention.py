"""Retention: archive old turns, then prune them from the hot store.

The hot store is Postgres, which the trace viewer reads. Retention is a two-step
operation so the audit trail is never lost:

1. every turn older than the cutoff is written to the configured archive backend;
2. only then are those exact turn ids deleted, and fully stale conversations dropped.

If the archive write fails, the turns are left in place and the error is raised: a
retention run that cannot archive must not delete. This is why the delete is by id
rather than by timestamp - a concurrent turn written between the read and the delete
is not swept up.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from blackbox.api.trace_archive import TraceArchive, turn_to_dict
from blackbox.logging_setup import get_logger

logger = get_logger(__name__)


def archive_and_prune(
    repository: Any,
    archive: TraceArchive,
    days: int,
    *,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Archive then delete turns older than ``days``.

    Returns counts, including how many turns were written to the archive, so an
    operator can verify the run before the data leaves the hot store.
    """
    if days < 0:
        raise ValueError("days must be >= 0")

    turns = repository.turns_older_than(days, tenant_id=tenant_id)
    archived = 0
    if turns and archive.enabled:
        grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for turn in turns:
            grouped[(turn.tenant_id, turn.conversation_id)].append(turn_to_dict(turn))
        for (turn_tenant, conversation_id), batch in grouped.items():
            archived += archive.write(turn_tenant, conversation_id, batch)
    elif turns:
        logger.warning(
            "retention_without_archive",
            turns=len(turns),
            detail="TRACE_ARCHIVE_BACKEND=none; pruning without preserving turns",
        )

    removed = repository.delete_turn_ids([turn.id for turn in turns])
    # Drop conversations whose last activity is older than the cutoff. Their turns
    # are gone either way; leaving the empty rows would leak tenant ids forever.
    conversations_removed = 0
    if tenant_id is None:
        conversations_removed = repository.delete_older_than(days)

    body = {
        "days": days,
        "tenant_id": tenant_id,
        "turns_archived": archived,
        "turns_removed": removed,
        "conversations_removed": conversations_removed,
        "archive_enabled": archive.enabled,
    }
    logger.info("retention_complete", **body)
    return body
