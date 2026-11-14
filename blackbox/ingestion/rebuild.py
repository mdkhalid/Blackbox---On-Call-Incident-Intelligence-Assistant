"""Rebuild a region's retrieval indexes from canonical documents.

Cross-region replication is the fast path, but it is not the source of truth: a region
whose index has drifted, or a newly promoted region whose replica lagged, is rebuilt
from the documents themselves. This module is the documented fallback in
`docs/scaling-and-production-readiness.md` §12.

The rebuild reuses the same staged-ingest machinery as a normal ingest: when the sparse
store has an alias, the new version is built behind it and the alias flips only after
validation; otherwise the dense and sparse stores are rebuilt and validated in place.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from blackbox.config.settings import AppConfig, Settings
from blackbox.logging_setup import get_logger

logger = get_logger(__name__)


def rebuild_region_indexes(
    settings: Settings,
    config: AppConfig,
    *,
    source_dir: Path,
    tenant_id: str = "default",
    force: bool = True,
) -> dict[str, Any]:
    """Rebuild this region's dense and sparse indexes from `source_dir`.

    `force` defaults to True: a rebuild is always a full rebuild. Reusing the dedupe
    skip would leave the new index missing every unchanged document.
    """
    from blackbox.ingestion.entity_extraction.store import SqlAlchemyEntityStore
    from blackbox.ingestion.pipeline import IngestionPipeline
    from blackbox.ingestion.staged import (
        IngestValidationError,
        run_staged_ingest,
        validate_ingest,
    )
    from blackbox.retrieval import build_default_retriever

    retriever = build_default_retriever(
        settings=settings, retrieval_config=config.retrieval, tenant_id=tenant_id
    )
    entity_store = SqlAlchemyEntityStore(_entity_url(settings))
    entity_store.init()

    def build_pipeline() -> IngestionPipeline:
        return IngestionPipeline(
            retriever.vectorstore,
            entity_store,
            sparse_store=getattr(retriever, "sparse", None),
            batch_size=config.ingestion.batch_size_chunks,
            tenant_id=tenant_id,
        )

    staged = run_staged_ingest(
        retriever,
        source_dir,
        tenant_id=tenant_id,
        pipeline_factory=build_pipeline,
    )
    if staged is not None:
        logger.info("region_rebuild_staged", region=settings.region, **staged.get("validation", {}))
        return {"region": settings.region, "staged": True, **staged}

    results = build_pipeline().ingest_directory(source_dir, force=force)
    report = validate_ingest(
        results,
        retriever.vectorstore,
        getattr(retriever, "sparse", None),
        tenants=(tenant_id,),
        expected_documents=len(results),
    )
    if not report.passed:
        raise IngestValidationError(report)
    logger.info("region_rebuild_committed", region=settings.region, **report.to_dict())
    return {
        "region": settings.region,
        "staged": False,
        "validation": report.to_dict(),
        "results": results,
    }


def _entity_url(settings: Settings) -> str:
    if settings.database_url:
        return settings.database_url
    Path("data").mkdir(exist_ok=True)
    return "sqlite:///data/entities.db"
