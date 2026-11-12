"""Cold trace storage and retention: archive before pruning, merge on read."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from blackbox.api.retention import archive_and_prune
from blackbox.api.store import SqlAlchemyConversationRepository
from blackbox.api.trace_archive import (
    FileTraceArchive,
    NullTraceArchive,
    S3TraceArchive,
    build_trace_archive,
)


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_object(self, *, Bucket, Key, Body):
        self.objects[Key] = Body

    def list_objects_v2(self, *, Bucket, Prefix, ContinuationToken=None):
        keys = sorted(key for key in self.objects if key.startswith(Prefix))
        return {"Contents": [{"Key": key} for key in keys], "IsTruncated": False}

    def get_object(self, *, Bucket, Key):
        return {"Body": SimpleNamespace(read=lambda: self.objects[Key])}


def _turn(turn_id: int, conversation_id: str = "c1", tenant_id: str = "t1") -> dict:
    return {
        "id": turn_id,
        "conversation_id": conversation_id,
        "tenant_id": tenant_id,
        "user_message": f"question {turn_id}",
        "assistant_message": f"answer {turn_id}",
        "status": "ok",
        "trace": [],
        "budget": {},
        "chunks": [],
        "created_at": f"2024-01-{turn_id:02d}T00:00:00+00:00",
    }


def test_file_archive_round_trips_and_deduplicates(tmp_path):
    archive = FileTraceArchive(tmp_path)
    assert archive.write("t1", "c1", [_turn(1)]) == 1
    assert archive.write("t1", "c1", [_turn(2)]) == 1
    # A retried batch must not produce a duplicate turn when read back.
    assert archive.write("t1", "c1", [_turn(2)]) == 1

    rows = archive.read("t1", "c1")
    assert [row["id"] for row in rows] == [1, 2]
    assert rows[0]["user_message"] == "question 1"


def test_file_archive_keeps_tenants_apart(tmp_path):
    archive = FileTraceArchive(tmp_path)
    archive.write("t1", "c1", [_turn(1, tenant_id="t1")])
    archive.write("t2", "c1", [_turn(2, tenant_id="t2")])

    assert [row["id"] for row in archive.read("t1", "c1")] == [1]
    assert [row["id"] for row in archive.read("t2", "c1")] == [2]
    assert archive.read("t3", "c1") == []


def test_archive_refuses_path_traversal(tmp_path):
    archive = FileTraceArchive(tmp_path)
    with pytest.raises(ValueError):
        archive.write("../escape", "c1", [_turn(1)])


def test_s3_archive_round_trips():
    client = FakeS3()
    archive = S3TraceArchive("bucket", prefix="traces", client=client)
    assert archive.write("t1", "c1", [_turn(1), _turn(2)]) == 2

    assert len(client.objects) == 1
    rows = archive.read("t1", "c1")
    assert [row["id"] for row in rows] == [1, 2]
    assert archive.read("t2", "c1") == []


def test_build_trace_archive_selects_the_configured_backend(tmp_path):
    assert not build_trace_archive(SimpleNamespace(trace_archive_backend="none")).enabled

    local = build_trace_archive(
        SimpleNamespace(
            trace_archive_backend="file", trace_archive_dir=str(tmp_path)
        )
    )
    assert isinstance(local, FileTraceArchive)

    with pytest.raises(ValueError):
        build_trace_archive(SimpleNamespace(trace_archive_backend="tape"))


class FailingArchive(NullTraceArchive):
    enabled = True

    def write(self, tenant_id, conversation_id, turns):
        raise OSError("archive is down")


def _repo(tmp_path) -> SqlAlchemyConversationRepository:
    repo = SqlAlchemyConversationRepository(f"sqlite:///{tmp_path / 'conv.db'}")
    repo.init()
    return repo


def _seed(repo, turns: int, *, tenant_id: str = "default") -> str:
    repo.get_or_create("c1", tenant_id=tenant_id)
    for i in range(turns):
        repo.append_turn(
            "c1", f"q{i}", f"a{i}", "ok", [], {}, tenant_id=tenant_id
        )
    return "c1"


def test_retention_archives_then_removes_the_same_turns(tmp_path):
    repo = _repo(tmp_path)
    _seed(repo, 3)
    archive = FileTraceArchive(tmp_path / "archive")

    body = archive_and_prune(repo, archive, days=0)

    assert body["turns_archived"] == 3
    assert body["turns_removed"] == 3
    assert body["archive_enabled"] is True
    assert len(archive.read("default", "c1")) == 3
    assert repo.get_turns("c1") == []


def test_retention_does_not_delete_when_the_archive_is_down(tmp_path):
    repo = _repo(tmp_path)
    _seed(repo, 2)

    with pytest.raises(OSError):
        archive_and_prune(repo, FailingArchive(), days=0)

    # The turns are still there: a failed archive must never become data loss.
    assert len(repo.get_turns("c1")) == 2


def test_retention_without_an_archive_still_prunes(tmp_path):
    repo = _repo(tmp_path)
    _seed(repo, 2)

    body = archive_and_prune(repo, NullTraceArchive(), days=0)

    assert body["turns_archived"] == 0
    assert body["turns_removed"] == 2
    assert repo.get_turns("c1") == []


def test_retention_never_touches_a_future_turn(tmp_path):
    repo = _repo(tmp_path)
    _seed(repo, 1)
    repo.append_turn("c1", "later", "later", "ok", [], {}, tenant_id="default")
    old = (datetime.now(UTC) - timedelta(days=40)).isoformat()
    future = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    with repo._session() as session:
        from blackbox.api.store import TurnRow

        rows = session.query(TurnRow).order_by(TurnRow.id).all()
        rows[0].created_at = old
        rows[1].created_at = future
        session.commit()

    body = archive_and_prune(repo, NullTraceArchive(), days=30)

    assert body["turns_removed"] == 1
    remaining = repo.get_turns("c1")
    assert [turn.user for turn in remaining] == ["later"]
