"""Conversation introspection: conversation, trace, memory and budget endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from blackbox.api.deps import get_services
from blackbox.api.services import PersistenceUnavailable

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


@router.get("/{conversation_id}")
def get_conversation(conversation_id: str, request: Request):
    services = _with_storage(request)
    record = _record_or_404(services, conversation_id)
    turns = services.conversations.get_turns(conversation_id)
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
def get_trace(conversation_id: str, request: Request):
    services = _with_storage(request)
    _record_or_404(services, conversation_id)
    turns = services.conversations.get_turns(conversation_id)
    return {
        "conversation_id": conversation_id,
        "turns": [
            {
                "id": turn.id,
                "user_message": turn.user,
                "assistant_message": turn.assistant,
                "status": turn.status,
                "trace": turn.trace,
                "budget": turn.budget,
                "chunks": turn.chunks,
                "created_at": turn.created_at,
            }
            for turn in turns
        ],
    }


@router.get("/{conversation_id}/memory")
def get_memory(conversation_id: str, request: Request):
    services = _with_storage(request)
    record = _record_or_404(services, conversation_id)
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


@router.get("/{conversation_id}/budget")
def get_budget(conversation_id: str, request: Request):
    services = _with_storage(request)
    record = _record_or_404(services, conversation_id)
    return record.last_budget or {}


def _with_storage(request: Request):
    services = get_services(request)
    try:
        services.ensure_storage()
    except PersistenceUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return services


def _record_or_404(services, conversation_id: str):
    record = services.conversations.get(conversation_id)
    if record is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return record
