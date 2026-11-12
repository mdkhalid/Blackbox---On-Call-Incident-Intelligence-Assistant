"""Cold storage for conversation turns ("trace store split").

Postgres holds the recent window that the trace viewer reads. Retention moves older
turns to an archive before deleting them, so the audit trail survives the pruning of
the hot store. Two archive backends are provided:

* ``file`` - one gzipped JSONL object per batch under a local directory. This is the
  development/test backend and also works for an NFS or mounted volume.
* ``s3`` - the same objects in an S3-compatible bucket (boto3 is the optional
  ``archive`` extra).

Both are deliberately simple: append-only objects, one batch per write, no updates.
A torn batch is therefore detectable, and re-running retention cannot corrupt an
existing object.
"""
from __future__ import annotations

import gzip
import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from blackbox.logging_setup import get_logger

logger = get_logger(__name__)

_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")

# The archived shape matches the /trace response so the reader merges the two
# sources without a second schema.
TURN_FIELDS = (
    "id",
    "conversation_id",
    "tenant_id",
    "user_message",
    "assistant_message",
    "status",
    "trace",
    "budget",
    "chunks",
    "created_at",
)


def turn_to_dict(turn: Any) -> dict[str, Any]:
    """Serialise a ``TurnRecord`` (or an object with the same fields)."""
    return {
        "id": turn.id,
        "conversation_id": turn.conversation_id,
        "tenant_id": turn.tenant_id,
        "user_message": turn.user,
        "assistant_message": turn.assistant,
        "status": turn.status,
        "trace": list(turn.trace or []),
        "budget": dict(turn.budget or {}),
        "chunks": list(turn.chunks or []),
        "created_at": turn.created_at,
    }


def normalise_turn(raw: dict[str, Any]) -> dict[str, Any]:
    """Keep only the archived fields, with a stable shape."""
    return {key: raw.get(key) for key in TURN_FIELDS}


class TraceArchive(Protocol):
    """Where old turns go before the hot store drops them."""

    enabled: bool

    def write(self, tenant_id: str, conversation_id: str, turns: list[dict]) -> int: ...

    def read(self, tenant_id: str, conversation_id: str) -> list[dict]: ...

    def close(self) -> None: ...


class NullTraceArchive:
    """Archive disabled: retention still prunes, but nothing is preserved."""

    enabled = False

    def write(self, tenant_id: str, conversation_id: str, turns: list[dict]) -> int:
        return 0

    def read(self, tenant_id: str, conversation_id: str) -> list[dict]:
        return []

    def close(self) -> None:
        return None


class FileTraceArchive:
    """Gzipped JSONL batches under a directory, one object per retention batch."""

    enabled = True

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _dir(self, tenant_id: str, conversation_id: str) -> Path:
        for name, value in (("tenant_id", tenant_id), ("conversation_id", conversation_id)):
            if not _SAFE_COMPONENT.fullmatch(value or ""):
                raise ValueError(f"unsafe {name} for an archive path")
        return self.root / f"tenant={tenant_id}" / f"conversation={conversation_id}"

    def _stamp(self) -> str:
        return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")

    def write(self, tenant_id: str, conversation_id: str, turns: list[dict]) -> int:
        if not turns:
            return 0
        directory = self._dir(tenant_id, conversation_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self._stamp()}-{uuid.uuid4().hex[:8]}.jsonl.gz"
        payload = "\n".join(
            json.dumps(normalise_turn(turn), default=str) for turn in turns
        )
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            handle.write(payload + "\n")
        logger.info(
            "trace_archive_write",
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            turns=len(turns),
            path=str(path),
        )
        return len(turns)

    def read(self, tenant_id: str, conversation_id: str) -> list[dict]:
        directory = self._dir(tenant_id, conversation_id)
        if not directory.is_dir():
            return []
        by_id: dict[Any, dict] = {}
        for path in sorted(directory.glob("*.jsonl.gz")):
            try:
                with gzip.open(path, "rt", encoding="utf-8") as handle:
                    for line in handle:
                        line = line.strip()
                        if not line:
                            continue
                        row = json.loads(line)
                        by_id[row.get("id")] = normalise_turn(row)
            except (OSError, ValueError):
                # A damaged batch must not make the whole archive unreadable; the
                # rest of the conversation is still there.
                logger.warning(
                    "trace_archive_read_failed",
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    path=str(path),
                )
                continue
        return sorted(by_id.values(), key=_order_key)

    def close(self) -> None:
        return None


class S3TraceArchive:
    """The same batches in an S3-compatible bucket."""

    enabled = True

    def __init__(
        self,
        bucket: str,
        *,
        prefix: str = "traces",
        region: str | None = None,
        client: Any | None = None,
    ) -> None:
        if not bucket:
            raise ValueError("an archive bucket is required")
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        if client is None:
            import boto3

            client = boto3.client("s3", region_name=region)
        self.client = client

    def _key(self, tenant_id: str, conversation_id: str) -> str:
        for name, value in (("tenant_id", tenant_id), ("conversation_id", conversation_id)):
            if not _SAFE_COMPONENT.fullmatch(value or ""):
                raise ValueError(f"unsafe {name} for an archive key")
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        batch = uuid.uuid4().hex[:8]
        return (
            f"{self.prefix}/tenant={tenant_id}/conversation={conversation_id}/"
            f"{stamp}-{batch}.jsonl.gz"
        )

    def write(self, tenant_id: str, conversation_id: str, turns: list[dict]) -> int:
        if not turns:
            return 0
        payload = "\n".join(
            json.dumps(normalise_turn(turn), default=str) for turn in turns
        )
        body = gzip.compress((payload + "\n").encode("utf-8"))
        key = self._key(tenant_id, conversation_id)
        self.client.put_object(Bucket=self.bucket, Key=key, Body=body)
        logger.info(
            "trace_archive_write",
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            turns=len(turns),
            key=key,
        )
        return len(turns)

    def read(self, tenant_id: str, conversation_id: str) -> list[dict]:
        prefix = f"{self.prefix}/tenant={tenant_id}/conversation={conversation_id}/"
        by_id: dict[Any, dict] = {}
        continuation: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": self.bucket, "Prefix": prefix}
            if continuation:
                kwargs["ContinuationToken"] = continuation
            response = self.client.list_objects_v2(**kwargs)
            for item in response.get("Contents") or []:
                raw = self.client.get_object(Bucket=self.bucket, Key=item["Key"])["Body"].read()
                try:
                    text = gzip.decompress(raw).decode("utf-8")
                except (OSError, ValueError):
                    logger.warning(
                        "trace_archive_read_failed",
                        tenant_id=tenant_id,
                        conversation_id=conversation_id,
                        key=item["Key"],
                    )
                    continue
                for line in text.splitlines():
                    if line.strip():
                        row = json.loads(line)
                        by_id[row.get("id")] = normalise_turn(row)
            if not response.get("IsTruncated"):
                break
            continuation = response.get("NextContinuationToken")
        return sorted(by_id.values(), key=_order_key)

    def close(self) -> None:
        return None


def _order_key(turn: dict) -> tuple[str, str]:
    created = turn.get("created_at") or ""
    return (str(created), str(turn.get("id")))


def build_trace_archive(settings: Any) -> TraceArchive:
    """Pick the configured archive backend. ``none`` is the safe default."""
    backend = (getattr(settings, "trace_archive_backend", "none") or "none").lower()
    if backend == "none":
        return NullTraceArchive()
    if backend == "file":
        return FileTraceArchive(getattr(settings, "trace_archive_dir", "./data/traces"))
    if backend == "s3":
        return S3TraceArchive(
            getattr(settings, "trace_archive_bucket", ""),
            prefix=getattr(settings, "trace_archive_prefix", "traces"),
            region=getattr(settings, "trace_archive_region", None),
        )
    raise ValueError(f"unknown TRACE_ARCHIVE_BACKEND: {backend}")
