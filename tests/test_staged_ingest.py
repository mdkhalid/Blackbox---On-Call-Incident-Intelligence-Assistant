"""Staged ingest: build a new index version, validate it, then flip the alias."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from blackbox.ingestion.staged import (
    IngestValidationError,
    run_staged_ingest,
    validate_ingest,
)
from blackbox.retrieval.opensearch import OpenSearchSparseStore


class FakeIndices:
    def __init__(self) -> None:
        self.indexes: set[str] = set()
        self.aliases: dict[str, str] = {}

    def exists(self, index: str) -> bool:
        return index in self.indexes

    def create(self, index: str, body=None) -> None:
        self.indexes.add(index)

    def delete(self, index: str) -> None:
        self.indexes.discard(index)

    def get_alias(self, index: str | None = None, name: str | None = None):
        if name is not None:
            return {value: {} for alias, value in self.aliases.items() if alias == name}
        if index is not None and index.endswith("-v*"):
            prefix = index[: -len("-v*")]
            return {name_: {} for name_ in self.indexes if name_.startswith(prefix + "-v")}
        return {}

    def update_aliases(self, body) -> None:
        for action in body["actions"]:
            if "remove" in action:
                self.aliases.pop(action["remove"]["alias"], None)
            if "add" in action:
                self.aliases[action["add"]["alias"]] = action["add"]["index"]


def _store() -> tuple[OpenSearchSparseStore, FakeIndices]:
    store = OpenSearchSparseStore(
        hosts="http://localhost:9200",
        index="blackbox_chunks",
        alias="blackbox_chunks",
    )
    indices = FakeIndices()
    store._client = SimpleNamespace(indices=indices)
    return store, indices


def test_begin_staged_writes_a_new_version_without_moving_reads():
    store, indices = _store()
    store.ensure_index()
    assert indices.aliases["blackbox_chunks"] == "blackbox_chunks-v1"

    version = store.begin_staged()

    assert version == 2
    assert "blackbox_chunks-v2" in indices.indexes
    assert store.write_target == "blackbox_chunks-v2"
    # Reads keep hitting the alias, which still points at the complete v1.
    assert store.index == "blackbox_chunks"
    assert indices.aliases["blackbox_chunks"] == "blackbox_chunks-v1"


def test_commit_staged_swaps_the_alias_and_clears_the_write_target():
    store, indices = _store()
    store.ensure_index()
    version = store.begin_staged()
    store.commit_staged(version)

    assert indices.aliases["blackbox_chunks"] == "blackbox_chunks-v2"
    assert store.write_target == "blackbox_chunks"
    assert store.write_index is None


def test_abort_staged_drops_only_the_new_version():
    store, indices = _store()
    store.ensure_index()
    store.begin_staged()
    store.abort_staged()

    assert "blackbox_chunks-v2" not in indices.indexes
    assert indices.aliases["blackbox_chunks"] == "blackbox_chunks-v1"
    assert store.write_index is None


def test_commit_without_a_staged_index_is_rejected():
    store, _indices = _store()
    store.ensure_index()
    with pytest.raises(RuntimeError, match="no staged index"):
        store.commit_staged(2)


def test_staging_requires_alias_mode():
    store = OpenSearchSparseStore(hosts="http://localhost:9200", index="plain")
    store._client = SimpleNamespace(indices=FakeIndices())
    with pytest.raises(RuntimeError, match="alias mode"):
        store.begin_staged()


class FakeVectorStore:
    def __init__(self, count: int = 10, tenant_count: int = 4) -> None:
        self._count = count
        self._tenant_count = tenant_count

    def count(self) -> int:
        return self._count

    def count_where(self, where: dict) -> int:
        return self._tenant_count if "tenant_id" in where else 0


class FakeSparse:
    alias = "blackbox_chunks"

    def __init__(self, staged_chunks: int = 3, tenant_chunks: int = 3) -> None:
        self.write_index: str | None = "blackbox_chunks-v2"
        self.staged_chunks = staged_chunks
        self.tenant_chunks = tenant_chunks
        self.committed: int | None = None
        self.aborted = False
        self.size = 99

    def begin_staged(self, version: int | None = None) -> int:
        self.write_index = "blackbox_chunks-v2"
        return 2

    def commit_staged(self, version: int) -> None:
        self.committed = version
        self.write_index = None

    def abort_staged(self) -> None:
        self.aborted = True
        self.write_index = None

    def count_staged(self) -> int:
        return self.staged_chunks

    def count_tenant(self, tenant_id: str, *, index: str | None = None) -> int:
        return self.tenant_chunks


def test_validation_passes_a_complete_ingest():
    report = validate_ingest(
        [{"status": "ingested", "chunks": 3}],
        FakeVectorStore(count=10, tenant_count=4),
        FakeSparse(staged_chunks=3, tenant_chunks=3),
        tenants=("t1",),
        expected_documents=1,
        staged_index="blackbox_chunks-v2",
    )
    assert report.passed, report.checks
    assert report.chunks_written == 3
    assert report.tenant_chunks == {"t1": 4}


def test_validation_fails_when_nothing_was_written():
    report = validate_ingest(
        [{"status": "ingested", "chunks": 0}],
        FakeVectorStore(count=10, tenant_count=0),
        FakeSparse(staged_chunks=0, tenant_chunks=0),
        tenants=("t1",),
        expected_documents=1,
        staged_index="blackbox_chunks-v2",
    )
    assert not report.passed
    assert report.checks["chunks_written"] is False
    assert report.checks["sparse_chunks"] is False
    assert report.checks["tenant_coverage"] is False


def test_validation_fails_when_a_tenant_is_missing():
    report = validate_ingest(
        [{"status": "ingested", "chunks": 3}],
        FakeVectorStore(count=10, tenant_count=0),
        FakeSparse(staged_chunks=3, tenant_chunks=0),
        tenants=("t1",),
        expected_documents=1,
        staged_index="blackbox_chunks-v2",
    )
    assert not report.passed
    assert report.checks["tenant_coverage"] is False


class FakePipeline:
    def __init__(self, results, calls: list[bool]) -> None:
        self._results = results
        self._calls = calls

    def ingest_directory(self, root, force: bool = False):
        self._calls.append(force)
        return self._results


def test_run_staged_ingest_commits_after_validation():
    sparse = FakeSparse()
    retriever = SimpleNamespace(vectorstore=FakeVectorStore(), sparse=sparse)
    calls: list[bool] = []

    outcome = run_staged_ingest(
        retriever,
        root="unused",  # type: ignore[arg-type]
        tenant_id="t1",
        pipeline_factory=lambda: FakePipeline(
            [{"status": "ingested", "chunks": 3}], calls
        ),
    )

    assert outcome is not None
    assert outcome["version"] == 2
    assert outcome["validation"]["passed"] is True
    assert sparse.committed == 2
    assert sparse.aborted is False
    # A staged build is always a full rebuild, whatever the caller asked for.
    assert calls == [True]


def test_run_staged_ingest_aborts_when_validation_fails():
    sparse = FakeSparse(staged_chunks=0, tenant_chunks=0)
    retriever = SimpleNamespace(vectorstore=FakeVectorStore(), sparse=sparse)

    with pytest.raises(IngestValidationError):
        run_staged_ingest(
            retriever,
            root="unused",  # type: ignore[arg-type]
            tenant_id="t1",
            pipeline_factory=lambda: FakePipeline(
                [{"status": "ingested", "chunks": 0}], []
            ),
        )

    assert sparse.aborted is True
    assert sparse.committed is None


def test_run_staged_ingest_aborts_when_the_pipeline_raises():
    sparse = FakeSparse()
    retriever = SimpleNamespace(vectorstore=FakeVectorStore(), sparse=sparse)

    def broken():
        raise OSError("loader failed")

    with pytest.raises(OSError):
        run_staged_ingest(
            retriever,
            root="unused",  # type: ignore[arg-type]
            tenant_id="t1",
            pipeline_factory=broken,
        )

    assert sparse.aborted is True
    assert sparse.committed is None


def test_run_staged_ingest_is_a_no_op_for_the_in_process_backend():
    retriever = SimpleNamespace(vectorstore=FakeVectorStore(), sparse=None)
    assert (
        run_staged_ingest(
            retriever, root="unused", tenant_id="t1", pipeline_factory=broken  # type: ignore[arg-type]
        )
        is None
    )


def broken():
    raise AssertionError("the pipeline must not be built without alias support")
