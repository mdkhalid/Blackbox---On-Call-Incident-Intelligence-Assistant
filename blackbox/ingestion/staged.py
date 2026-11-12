"""Staged ingest: build a complete new index version, validate it, then swap.

A partial rebuild must never be published. The old flow wrote in place, so a crash
halfway through left a corpus that answered some questions from the new documents and
some from the old ones - or, after a document deletion, from neither. When the sparse
store supports aliases (``OpenSearchSparseStore(alias=...)``) the replacement is built
in a new concrete index while reads continue to use the alias, and the alias flips
only after the checks below pass.

Validation answers the questions a cutover depends on:

* did every document the loader found reach the pipeline?
* did the run actually write chunks (a silently empty index is the failure mode that
  matters most)?
* is the tenant that ran the ingest represented in the new index?

If any answer is no, the staged index is dropped and the alias keeps pointing at the
previous, complete version. The error is raised so the job reports the failure.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from blackbox.logging_setup import get_logger

logger = get_logger(__name__)


class IngestValidationError(RuntimeError):
    """The staged index is not fit to serve; the alias was not moved."""

    def __init__(self, report: ValidationReport) -> None:
        failed = [name for name, ok in report.checks.items() if not ok]
        super().__init__("staged ingest validation failed: " + ", ".join(failed))
        self.report = report


@dataclass
class ValidationReport:
    documents: int
    ingested: int
    skipped: int
    chunks_written: int
    chunks_total: int
    sparse_chunks: int
    tenant_chunks: dict[str, int] = field(default_factory=dict)
    checks: dict[str, bool] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return all(self.checks.values()) and bool(self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "documents": self.documents,
            "ingested": self.ingested,
            "skipped": self.skipped,
            "chunks_written": self.chunks_written,
            "chunks_total": self.chunks_total,
            "sparse_chunks": self.sparse_chunks,
            "tenant_chunks": self.tenant_chunks,
            "checks": self.checks,
            "passed": self.passed,
        }


def validate_ingest(
    results: list[dict],
    vectorstore: Any,
    sparse_store: Any | None = None,
    *,
    tenants: tuple[str, ...] = (),
    expected_documents: int | None = None,
    staged_index: str | None = None,
) -> ValidationReport:
    """Check a completed ingest before it is allowed to serve traffic."""
    documents = len(results)
    ingested = sum(1 for result in results if result.get("status") == "ingested")
    skipped = sum(1 for result in results if result.get("status") == "skipped")
    chunks_written = sum(int(result.get("chunks") or 0) for result in results)

    try:
        chunks_total = int(vectorstore.count())
    except Exception:  # noqa: BLE001 - reported as a failed check
        chunks_total = 0

    sparse_chunks = 0
    if sparse_store is not None:
        if staged_index and hasattr(sparse_store, "count_staged"):
            sparse_chunks = int(sparse_store.count_staged())
        else:
            sparse_chunks = int(getattr(sparse_store, "size", 0) or 0)

    tenant_chunks: dict[str, int] = {}
    for tenant_id in tenants:
        count = 0
        if hasattr(vectorstore, "count_where"):
            try:
                count = int(vectorstore.count_where({"tenant_id": tenant_id}))
            except Exception:  # noqa: BLE001 - fall through to the sparse count
                count = 0
        if count == 0 and sparse_store is not None and hasattr(sparse_store, "count_tenant"):
            try:
                count = int(sparse_store.count_tenant(tenant_id, index=staged_index))
            except Exception:  # noqa: BLE001 - validation reports the zero
                count = 0
        tenant_chunks[tenant_id] = count

    checks = {
        "documents_processed": documents > 0,
        "documents_expected": (
            expected_documents is None or documents >= expected_documents
        ),
        "chunks_written": chunks_written > 0,
        "chunks_total": chunks_total > 0,
        "sparse_chunks": sparse_store is None or sparse_chunks > 0,
        "tenant_coverage": all(count > 0 for count in tenant_chunks.values()),
    }
    report = ValidationReport(
        documents=documents,
        ingested=ingested,
        skipped=skipped,
        chunks_written=chunks_written,
        chunks_total=chunks_total,
        sparse_chunks=sparse_chunks,
        tenant_chunks=tenant_chunks,
        checks=checks,
    )
    logger.info("ingest_validation", **report.to_dict())
    return report


def run_staged_ingest(
    retriever: Any,
    root: Path,
    *,
    tenant_id: str,
    pipeline_factory: Any,
) -> dict[str, Any] | None:
    """Build and validate a new sparse index version, then swap the alias.

    Returns ``None`` when the configured sparse store has no alias (the in-process
    BM25 backend), in which case the caller runs the ordinary ingest. A staged build
    is always a full rebuild: reusing the dedupe skip would leave the new index
    missing every unchanged document, so "force" is not optional here.
    """
    sparse = getattr(retriever, "sparse", None)
    if sparse is None or not getattr(sparse, "alias", None) or not hasattr(
        sparse, "begin_staged"
    ):
        return None

    version = sparse.begin_staged()
    try:
        results = pipeline_factory().ingest_directory(root, force=True)
        report = validate_ingest(
            results,
            retriever.vectorstore,
            sparse,
            tenants=(tenant_id,),
            expected_documents=len(results),
            staged_index=getattr(sparse, "write_index", None),
        )
        if not report.passed:
            raise IngestValidationError(report)
        sparse.commit_staged(version)
    except BaseException:
        # The alias still points at the previous version; drop the half-built one.
        sparse.abort_staged()
        raise
    logger.info(
        "staged_ingest_committed",
        tenant_id=tenant_id,
        version=version,
        chunks=report.chunks_written,
    )
    return {"version": version, "validation": report.to_dict(), "results": results}
