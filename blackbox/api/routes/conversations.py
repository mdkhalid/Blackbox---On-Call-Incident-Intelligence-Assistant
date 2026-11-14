"""Conversation introspection: conversation, trace, memory and budget endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from blackbox.api.auth import Principal, require_roles
from blackbox.api.deps import get_services
from blackbox.api.regions import WrongRegion
from blackbox.api.services import PersistenceUnavailable
from blackbox.api.trace_archive import turn_to_dict
from blackbox.observability.cost import conversation_cost

router = APIRouter(prefix="/api/conversations", tags=["conversations"])

_READ_ROLES = require_roles("viewer", "operator", "admin")
_READ_AUTH = Depends(_READ_ROLES)


@router.get("/{conversation_id}")
def get_conversation(
    conversation_id: str,
    request: Request,
    principal: Principal = _READ_AUTH,
):
    services = _with_storage(request)
    record = _record_or_404(services, conversation_id, principal.tenant_id)
    turns = services.conversations.get_turns(
        conversation_id, tenant_id=principal.tenant_id
    )
    last = turns[-1] if turns else None
    return {
        "id": record.id,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "turns": len(turns),
        "last_status": last.status if last else None,
        "last_message": last.user if last else None,
        "summary": record.memory.summary.text,
        "compression_count": record.memory.summary.compression_count,
    }


@router.get("/{conversation_id}/trace")
def get_trace(
    conversation_id: str,
    request: Request,
    principal: Principal = _READ_AUTH,
    limit: int = 100,
    offset: int = 0,
    include_archive: bool = False,
):
    """A page of the conversation's turns, optionally merged with archived turns.

    Postgres holds the recent window; turns older than the retention cutoff live in
    the archive. ``include_archive`` reads both and paginates the merged, ordered
    list, which is what the trace viewer needs when it scrolls past the hot window.
    """
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be >= 0")

    services = _with_storage(request)
    _record_or_404(services, conversation_id, principal.tenant_id)

    recent = services.conversations.get_turns(
        conversation_id, tenant_id=principal.tenant_id
    )
    page: list[dict]
    total: int
    archive = services.get_trace_archive() if include_archive else None
    if archive is not None and archive.enabled:
        archived = archive.read(principal.tenant_id, conversation_id)
        # The hot store wins when an id exists in both: it is the authoritative copy
        # and a duplicate can only mean an interrupted prune.
        merged: dict[object, dict] = {row.get("id"): row for row in archived}
        merged.update({turn.id: turn_to_dict(turn) for turn in recent})
        ordered = sorted(
            merged.values(),
            key=lambda row: (str(row.get("created_at") or ""), str(row.get("id"))),
        )
        total = len(ordered)
        page = ordered[offset : offset + limit]
    else:
        total = services.conversations.count_turns(
            conversation_id, tenant_id=principal.tenant_id
        )
        page = [
            turn_to_dict(turn)
            for turn in services.conversations.get_turns_page(
                conversation_id,
                tenant_id=principal.tenant_id,
                limit=limit,
                offset=offset,
            )
        ]

    return {
        "conversation_id": conversation_id,
        "limit": limit,
        "offset": offset,
        "total": total,
        "returned": len(page),
        "has_more": offset + len(page) < total,
        "turns": page,
    }


@router.get("/{conversation_id}/memory")
def get_memory(
    conversation_id: str,
    request: Request,
    principal: Principal = _READ_AUTH,
):
    services = _with_storage(request)
    record = _record_or_404(services, conversation_id, principal.tenant_id)
    memory = record.memory
    return {
        "conversation_id": conversation_id,
        "entity": memory.entity.to_dict(),
        "summary": {
            "text": memory.summary.text,
            "compression_count": memory.summary.compression_count,
            "turns_since_compression": memory.summary.turns_since_compression,
        },
        "working": [
            {
                "user": turn.user,
                "assistant": turn.assistant,
                "timestamp": turn.timestamp,
            }
            for turn in memory.working.recent()
        ],
        "last_budget": record.last_budget,
    }


@router.get("/{conversation_id}/cost")
def get_cost(
    conversation_id: str,
    request: Request,
    principal: Principal = _READ_AUTH,
):
    """Per-node LLM cost for the conversation, derived from its stored traces."""
    services = _with_storage(request)
    _record_or_404(services, conversation_id, principal.tenant_id)
    turns = services.conversations.get_turns(
        conversation_id, tenant_id=principal.tenant_id
    )
    report = conversation_cost([turn.trace for turn in turns], services.config.cost)
    return {"conversation_id": conversation_id, **report}


@router.get("/{conversation_id}/budget")
def get_budget(
    conversation_id: str,
    request: Request,
    principal: Principal = _READ_AUTH,
):
    services = _with_storage(request)
    record = _record_or_404(services, conversation_id, principal.tenant_id)
    return record.last_budget or {}


def _guard_region(services, tenant_id: str) -> None:
    """Reject a home-region read that reached the wrong region (HTTP 421)."""
    if not services.settings.region_write_pinning:
        return
    try:
        services.tenant_region_resolver().assert_write_region(tenant_id)
    except WrongRegion as exc:
        raise HTTPException(
            status_code=421,
            detail=f"tenant {tenant_id} is homed in region {exc.home_region}",
            headers={"X-Blackbox-Home-Region": exc.home_region},
        ) from exc


def _with_storage(request: Request):
    services = get_services(request)
    try:
        services.ensure_storage()
    except PersistenceUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return services


def _record_or_404(services, conversation_id: str, tenant_id: str):
    _guard_region(services, tenant_id)
    record = services.conversations.get(conversation_id, tenant_id=tenant_id)
    if record is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return record
