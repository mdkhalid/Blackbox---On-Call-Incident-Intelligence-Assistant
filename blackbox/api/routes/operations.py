"""Operational endpoints: corpus ingestion and retrieval evaluation.

Both are long jobs, so both answer `202 {job_id}` and run the work on a background
worker. The status endpoints report what the job did.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from blackbox.api.auth import Principal, require_roles
from blackbox.api.deps import get_services
from blackbox.api.jobs import register_handler
from blackbox.api.schemas import EvalRequest, IngestRequest
from blackbox.eval.groundedness_eval import evaluate_dataset
from blackbox.eval.retrieval_eval import fusion_sweep, load_dataset, run_all
from blackbox.ingestion.pipeline import IngestionPipeline
from blackbox.logging_setup import get_logger

logger = get_logger(__name__)
router = APIRouter(tags=["operations"])
_ADMIN_AUTH = Depends(require_roles("admin"))
_READ_AUTH = Depends(require_roles("viewer", "operator", "admin"))


@router.post("/api/ingest", status_code=202)
async def ingest(
    payload: IngestRequest,
    request: Request,
    principal: Principal = _ADMIN_AUTH,
):
    services = get_services(request)
    logger.info(
        "audit_admin_action",
        action="ingest",
        tenant_id=principal.tenant_id,
        subject=principal.subject,
        corpus_dir=payload.corpus_dir,
    )

    root = _corpus_path(payload.corpus_dir)
    if not root.is_dir():
        raise HTTPException(status_code=400, detail="corpus_dir does not exist")

    job = services.submit_job(
        "ingest",
        {"corpus_dir": str(root), "refresh": payload.refresh},
        tenant_id=principal.tenant_id,
        subject=principal.subject,
    )
    return _job_response(job, "/api/ingest")


def _run_ingest(services, params: dict[str, Any]) -> dict[str, Any]:
    """Ingest one corpus directory. Runs on a worker, never on the request path."""
    root = Path(params["corpus_dir"])
    refresh = bool(params.get("refresh", False))
    services.ensure_runtime()
    if services.retriever is None or services.entity_store is None:
        raise RuntimeError("ingestion dependencies unavailable")
    pipeline = IngestionPipeline(
        services.retriever.vectorstore,
        services.entity_store,
        sparse_store=services.retriever.sparse,
        batch_size=services.config.ingestion.batch_size_chunks,
        shadow_store=getattr(services.retriever, "shadow_vectorstore", None),
    )
    results = pipeline.ingest_directory(root, force=refresh)
    # The corpus-derived indexes are rebuilt here, on the worker, and published
    # atomically. A rebuild is O(corpus), so it must not happen on the request
    # path of the next query.
    services.retriever.rebuild_indexes()
    ingested = sum(1 for result in results if result["status"] == "ingested")
    skipped = sum(1 for result in results if result["status"] == "skipped")
    body = {
        "corpus_dir": str(root),
        "documents": len(results),
        "ingested": ingested,
        "skipped": skipped,
        "chunks": services.retriever.vectorstore.count(),
        "forced": refresh,
    }
    logger.info("ingest_complete", **body)
    return body


@router.get("/api/ingest/{job_id}")
def ingest_status(
    job_id: str,
    request: Request,
    principal: Principal = _READ_AUTH,
):
    return _job_status(job_id, request, principal, kind="ingest")


@router.post("/api/eval/run", status_code=202)
async def run_evaluation(
    payload: EvalRequest,
    request: Request,
    principal: Principal = _ADMIN_AUTH,
):
    services = get_services(request)
    logger.info(
        "audit_admin_action",
        action="eval",
        tenant_id=principal.tenant_id,
        subject=principal.subject,
        k=payload.k,
    )

    job = services.submit_job(
        "eval",
        payload.model_dump(),
        tenant_id=principal.tenant_id,
        subject=principal.subject,
    )
    return _job_response(job, "/api/eval")


async def _run_eval(services, params: dict[str, Any]) -> dict[str, Any]:
    """Run the retrieval (and optional groundedness) evaluation on a worker."""
    k = int(params.get("k", 5))
    await asyncio.to_thread(services.ensure_runtime)
    retriever = services.retriever
    if retriever is None:
        raise RuntimeError("runtime unavailable")
    dataset = load_dataset()
    results = await asyncio.to_thread(run_all, retriever, dataset, k=k)
    strategies = {name: asdict(metrics) for name, (metrics, _) in results.items()}
    body: dict[str, Any] = {
        "k": k,
        "questions": dataset.get("question_count"),
        "strategies": strategies,
    }
    if params.get("fusion_sweep"):
        sweep = await asyncio.to_thread(fusion_sweep, retriever, dataset, k=k)
        body["fusion_sweep"] = [
            {"dense_weight": dense, "sparse_weight": sparse, "mrr": mrr}
            for dense, sparse, mrr in sweep
        ]
    if params.get("groundedness"):
        body["groundedness"] = await evaluate_dataset(
            services.graph, dataset, limit=params.get("limit")
        )
    return body


@router.get("/api/eval/{job_id}")
def eval_status(
    job_id: str,
    request: Request,
    principal: Principal = _READ_AUTH,
):
    return _job_status(job_id, request, principal, kind="eval")


@router.get("/api/jobs/{job_id}")
def job_status(
    job_id: str,
    request: Request,
    principal: Principal = _READ_AUTH,
):
    return _job_status(job_id, request, principal, kind=None)


def _job_response(job, status_url: str) -> dict[str, Any]:
    return {
        "job_id": job.id,
        "kind": job.kind,
        "status": job.status,
        "submitted_at": job.submitted_at,
        "status_url": f"{status_url}/{job.id}",
    }


def _job_status(job_id: str, request: Request, principal: Principal, *, kind: str | None):
    services = get_services(request)
    found = services.find_job(job_id, kind=kind)
    if found is None:
        raise HTTPException(status_code=404, detail="job not found")
    queue, job = found
    # A job that belongs to another tenant is reported as missing rather than as
    # forbidden, so the endpoint cannot be used to probe for other tenants' jobs.
    if job.tenant_id != principal.tenant_id:
        raise HTTPException(status_code=404, detail="job not found")
    body = queue.snapshot(job)
    body["status_url"] = f"/api/jobs/{job.id}"
    return body


@router.delete("/api/admin/tenants/{tenant_id}", status_code=200)
async def offboard_tenant(
    tenant_id: str,
    request: Request,
    principal: Principal = _ADMIN_AUTH,
):
    """Offboard a tenant: remove its conversations, turns and index payloads."""
    if not tenant_id or tenant_id == "default":
        raise HTTPException(status_code=400, detail="invalid tenant_id")
    services = get_services(request)
    try:
        await asyncio.to_thread(services.ensure_storage)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    removed = await asyncio.to_thread(services.conversations.delete_tenant, tenant_id)
    logger.info(
        "audit_admin_action",
        action="tenant_offboard",
        tenant_id=tenant_id,
        conversations_removed=removed,
    )
    return {"tenant_id": tenant_id, "conversations_removed": removed}


@router.post("/api/admin/retention", status_code=200)
async def run_retention(
    request: Request,
    response: Response,
    days: int = 90,
    archive: bool = False,
    background: bool = False,
    principal: Principal = _ADMIN_AUTH,
):
    """Prune turns older than ``days``, archiving them first when requested.

    ``background=true`` queues the run on the job worker (and answers 202), which is
    what an external scheduler should use: retention can outlive a request when the
    corpus is large.
    """
    if days < 0:
        raise HTTPException(status_code=400, detail="days must be >= 0")
    services = get_services(request)
    try:
        await asyncio.to_thread(services.ensure_storage)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if background:
        job = services.submit_job(
            "retention",
            {"days": days, "archive": archive},
            tenant_id=principal.tenant_id,
            subject=principal.subject,
        )
        response.status_code = 202
        return _job_response(job, "/api/admin/retention")

    body = await asyncio.to_thread(_retention_body, services, days, archive)
    logger.info("audit_admin_action", action="retention", **body)
    return body


def _retention_body(services, days: int, archive: bool) -> dict[str, Any]:
    if archive:
        from blackbox.api.retention import archive_and_prune

        return archive_and_prune(
            services.conversations, services.get_trace_archive(), days
        )
    removed = services.conversations.delete_older_than(days)
    return {"days": days, "conversations_removed": removed}


def _run_retention(services, params: dict[str, Any]) -> dict[str, Any]:
    services.ensure_storage()
    body = _retention_body(
        services, int(params.get("days", 90)), bool(params.get("archive", False))
    )
    logger.info("audit_admin_action", action="retention", **body)
    return body


register_handler("ingest", _run_ingest)
register_handler("eval", _run_eval)
register_handler("retention", _run_retention)


def _corpus_path(raw: str) -> Path:
    base = Path.cwd().resolve()
    candidate = Path(raw)
    resolved = (candidate if candidate.is_absolute() else base / candidate).resolve()
    if not resolved.is_relative_to(base):
        raise HTTPException(
            status_code=400, detail="corpus_dir must stay inside the project workspace"
        )
    return resolved
