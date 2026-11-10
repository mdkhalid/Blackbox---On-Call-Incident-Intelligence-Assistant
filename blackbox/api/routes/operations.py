"""Operational endpoints: corpus ingestion and retrieval evaluation."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from blackbox.api.deps import get_services
from blackbox.api.schemas import EvalRequest, IngestRequest
from blackbox.eval.retrieval_eval import fusion_sweep, load_dataset, run_all
from blackbox.ingestion.pipeline import IngestionPipeline
from blackbox.logging_setup import get_logger

logger = get_logger(__name__)
router = APIRouter(tags=["operations"])


@router.post("/api/ingest")
def ingest(payload: IngestRequest, request: Request):
    services = get_services(request)
    try:
        services.ensure_runtime()
    except Exception as exc:
        logger.exception("ingest_runtime_unavailable")
        raise HTTPException(status_code=503, detail="runtime unavailable") from exc

    root = _corpus_path(payload.corpus_dir)
    if not root.is_dir():
        raise HTTPException(status_code=400, detail="corpus_dir does not exist")

    if not services.ingest_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="an ingestion job is already running")
    try:
        if services.retriever is None or services.entity_store is None:
            raise HTTPException(status_code=503, detail="ingestion dependencies unavailable")
        pipeline = IngestionPipeline(
            services.retriever.vectorstore,
            services.entity_store,
            batch_size=services.config.ingestion.batch_size_chunks,
        )
        results = pipeline.ingest_directory(root, force=payload.refresh)
        services.retriever.invalidate()
        ingested = sum(1 for result in results if result["status"] == "ingested")
        skipped = sum(1 for result in results if result["status"] == "skipped")
        body = {
            "corpus_dir": str(root),
            "documents": len(results),
            "ingested": ingested,
            "skipped": skipped,
            "chunks": services.retriever.vectorstore.count(),
            "forced": payload.refresh,
        }
        logger.info("ingest_complete", **body)
        return body
    finally:
        services.ingest_lock.release()


@router.post("/api/eval/run")
def run_evaluation(payload: EvalRequest, request: Request):
    services = get_services(request)
    try:
        services.ensure_runtime()
    except Exception as exc:
        logger.exception("eval_runtime_unavailable")
        raise HTTPException(status_code=503, detail="runtime unavailable") from exc

    if not services.eval_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="an evaluation is already running")
    try:
        try:
            dataset = load_dataset()
        except SystemExit as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        results = run_all(services.retriever, dataset, k=payload.k)
        strategies = {
            name: asdict(metrics) for name, (metrics, _) in results.items()
        }
        body = {
            "k": payload.k,
            "questions": dataset.get("question_count"),
            "strategies": strategies,
        }
        if payload.fusion_sweep:
            sweep = fusion_sweep(services.retriever, dataset, k=payload.k)
            body["fusion_sweep"] = [
                {"dense_weight": dense, "sparse_weight": sparse, "mrr": mrr}
                for dense, sparse, mrr in sweep
            ]
        return body
    finally:
        services.eval_lock.release()


def _corpus_path(raw: str) -> Path:
    base = Path.cwd().resolve()
    candidate = Path(raw)
    resolved = (candidate if candidate.is_absolute() else base / candidate).resolve()
    if not resolved.is_relative_to(base):
        raise HTTPException(
            status_code=400, detail="corpus_dir must stay inside the project workspace"
        )
    return resolved
