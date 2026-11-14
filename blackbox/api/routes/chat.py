"""POST /api/chat — SSE streaming of graph events, then the grounded answer."""
from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from blackbox.api.auth import Principal, require_roles
from blackbox.api.deps import get_services
from blackbox.api.guardrails import detect_injection
from blackbox.api.locks import ConversationBusy
from blackbox.api.regions import WrongRegion
from blackbox.api.schemas import ChatRequest
from blackbox.api.services import PersistenceUnavailable
from blackbox.api.sse import (
    ClientDisconnected,
    chunk_trails,
    public_update,
    sse_event,
    stream_with_disconnect,
    token_chunks,
)
from blackbox.api.store import ConversationTenantMismatch
from blackbox.graph import initial_state, stream_graph_events
from blackbox.graph.prompts import CHAT_SYSTEM_PROMPT
from blackbox.logging_setup import get_logger
from blackbox.memory import build_context
from blackbox.memory.updates import update_entity_memory
from blackbox.observability.cost import turn_cost
from blackbox.observability.metrics import SSE_ACTIVE
from blackbox.observability.tracing import span
from blackbox.pii import summarize_query

logger = get_logger(__name__)
router = APIRouter(tags=["chat"])

_CONVERSATION_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_IDEMPOTENCY_HEADER = Header(default=None, alias="Idempotency-Key")

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}
_CHAT_AUTH = Depends(require_roles("operator", "admin"))


@router.post("/api/chat")
async def chat(
    payload: ChatRequest,
    request: Request,
    principal: Principal = _CHAT_AUTH,
    idempotency_key: str | None = _IDEMPOTENCY_HEADER,
):
    services = get_services(request)
    tenant_id = principal.tenant_id
    message = payload.message
    if not message.strip():
        raise HTTPException(status_code=400, detail="message must not be empty")
    if len(message) > services.config.guardrails.max_message_chars:
        raise HTTPException(
            status_code=413,
            detail=(
                "message exceeds the configured limit of "
                f"{services.config.guardrails.max_message_chars} characters"
            ),
        )

    conversation_id = (payload.conversation_id or uuid.uuid4().hex).strip()
    if not _CONVERSATION_ID.fullmatch(conversation_id):
        raise HTTPException(status_code=400, detail="invalid conversation_id")

    # Query logging is policy-controlled: hashed by default, redacted or raw on
    # explicit opt-in. The raw user text never reaches a log line otherwise.
    logger.debug(
        "chat_query",
        conversation_id=conversation_id,
        query=summarize_query(message, services.settings.query_log_mode),
    )

    injection_flags = detect_injection(message)
    if injection_flags:
        logger.warning(
            "prompt_injection_suspected",
            conversation_id=conversation_id,
            flags=injection_flags,
        )
        if services.config.guardrails.strict_injection_block:
            raise HTTPException(
                status_code=400, detail="message rejected by input guardrail"
            )

    try:
        await run_in_threadpool(services.ensure_storage)
        await run_in_threadpool(services.ensure_runtime)
    except PersistenceUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("runtime_unavailable")
        raise HTTPException(status_code=503, detail="runtime unavailable") from exc

    # Tenant-pinned writes: a conversation belongs to one home region. If this
    # instance does not own the tenant, tell the edge where to retry rather than
    # writing another region's database.
    if services.settings.region_write_pinning:
        resolver = services.tenant_region_resolver()
        try:
            await run_in_threadpool(resolver.assert_write_region, tenant_id)
        except WrongRegion as exc:
            raise HTTPException(
                status_code=421,
                detail=f"tenant {tenant_id} is homed in region {exc.home_region}",
                headers={"X-Blackbox-Home-Region": exc.home_region},
            ) from exc

    cache = services.cache_backend
    idempotency_cache_key: str | None = None
    idempotency_lock_key: str | None = None

    async def _release_idempotency_lock() -> None:
        if idempotency_lock_key and cache is not None:
            await run_in_threadpool(cache.delete, idempotency_lock_key)

    if idempotency_key is not None:
        if not _IDEMPOTENCY_KEY.fullmatch(idempotency_key):
            raise HTTPException(status_code=400, detail="invalid Idempotency-Key")
        if cache is not None:
            idempotency_cache_key = f"idem:{tenant_id}:{idempotency_key}"
            cached = await run_in_threadpool(cache.get, idempotency_cache_key)
            if isinstance(cached, dict):
                return StreamingResponse(
                    _replay_final(cached),
                    media_type="text/event-stream",
                    headers=_SSE_HEADERS,
                )
            idempotency_lock_key = f"idem-lock:{tenant_id}:{idempotency_key}"
            acquired = await run_in_threadpool(
                cache.set_if_absent, idempotency_lock_key, "1", 60
            )
            if not acquired:
                raise HTTPException(
                    status_code=409,
                    detail="request with this Idempotency-Key is already in progress",
                    headers={"Retry-After": "1"},
                )

    if services.settings.tenant_quota_enabled:
        quota = await services.ensure_tenant_quota()
        allowed, reason, retry_after = await quota.check(tenant_id)
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail=f"tenant quota exceeded: {reason}",
                headers={"Retry-After": str(retry_after or 1)},
            )

    # Two layers: a per-pod counter rejects instantly without a Redis round trip, and
    # the shared slot counter enforces the same limit across every replica.
    if not services.try_acquire_tenant(tenant_id):
        await _release_idempotency_lock()
        raise HTTPException(
            status_code=429,
            detail="tenant concurrency limit reached",
            headers={"Retry-After": "1"},
        )
    if not await services.acquire_tenant_slot(tenant_id):
        services.release_tenant(tenant_id)
        await _release_idempotency_lock()
        raise HTTPException(
            status_code=429,
            detail="tenant concurrency limit reached",
            headers={"Retry-After": "1"},
        )

    try:
        turn_lock = await services.acquire_turn_lock(conversation_id, tenant_id)
    except ConversationBusy as exc:
        services.release_tenant(tenant_id)
        await services.release_tenant_slot(tenant_id)
        await _release_idempotency_lock()
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    try:
        record = await run_in_threadpool(
            services.conversations.get_or_create,
            conversation_id,
            tenant_id=tenant_id,
        )
    except ConversationTenantMismatch as exc:
        await turn_lock.release()
        services.release_tenant(tenant_id)
        await services.release_tenant_slot(tenant_id)
        await _release_idempotency_lock()
        raise HTTPException(status_code=404, detail="conversation not found") from exc
    except Exception:
        await turn_lock.release()
        services.release_tenant(tenant_id)
        await services.release_tenant_slot(tenant_id)
        await _release_idempotency_lock()
        raise

    async def event_stream():
        SSE_ACTIVE.inc()
        with span(
            "chat.turn",
            **{
                "tenant.id": tenant_id,
                "conversation.id": conversation_id,
                "chat.idempotent": idempotency_key is not None,
            },
        ) as turn_span:
            try:
                memory = record.memory
                if memory.compression_due:
                    await run_in_threadpool(
                        memory.maybe_compress,
                        services.llm,
                        model=services.settings.grading_model,
                    )

                bundle = build_context(
                    system_prompt=CHAT_SYSTEM_PROMPT,
                    memory=memory,
                    chunks=[],
                    question=message,
                    budget=services.config.budget,
                )
                state = initial_state(
                    message,
                    context=bundle.render(),
                    budget=bundle.report,
                    filters=payload.filters,
                    injection_flags=injection_flags,
                    tenant_id=tenant_id,
                )

                result: dict = {}
                async for event in stream_with_disconnect(
                    request, stream_graph_events(services.graph, state)
                ):
                    if event["event"] == "node":
                        yield sse_event(
                            "node",
                            {
                                "node": event["node"],
                                "data": public_update(event.get("data") or {}),
                            },
                        )
                    elif event["event"] == "final":
                        result = event.get("data") or {}

                # Tokens are emitted only here, after the graph has finished. The `generate`
                # node is the first to produce an answer, but the ground check runs after it
                # and strips claims that no retrieved chunk supports. Emitting from the node
                # stream would put ungrounded text on the wire before the check runs, which
                # is exactly the failure the ground check exists to prevent. The answer below
                # is the post-ground-check text (or the honest-failure text), so what the
                # client renders is what the server actually stands behind.
                answer = result.get("answer") or ""
                for piece in token_chunks(answer):
                    yield sse_event("token", {"text": piece})

                # The graph reports the allocation of the request it actually sent, so the
                # budget endpoint reports the same numbers the provider received rather
                # than a second, independent estimate of them.
                budget = result.get("budget") or bundle.report
                memory.add_turn(message, answer)
                memory.last_budget = budget
                entity_events = update_entity_memory(
                    memory.entity, user_message=message, assistant_message=answer
                )
                if entity_events:
                    logger.info(
                        "entity_memory_updated",
                        conversation_id=conversation_id,
                        events=entity_events,
                    )

                with span(
                    "db.record_turn",
                    **{"db.operation": "record_turn", "tenant.id": tenant_id},
                ) as db_span:
                    await run_in_threadpool(
                        services.conversations.record_turn,
                        conversation_id,
                        memory,
                        budget,
                        message,
                        answer,
                        result.get("status") or "error",
                        result.get("trace") or [],
                        result.get("chunks") or [],
                        tenant_id=tenant_id,
                    )
                db_span.set_attribute(
                    "db.trace_events", len(result.get("trace") or [])
                )
                db_span.set_attribute(
                    "db.status", str(result.get("status") or "error")
                )

                if services.settings.tenant_quota_enabled:
                    quota = await services.ensure_tenant_quota()
                    cost = turn_cost(
                        result.get("trace") or [], services.config.cost
                    )["total_usd"]
                    await quota.record(
                        tenant_id,
                        tokens=int(
                            (result.get("usage") or {}).get("total_tokens") or 0
                        ),
                        llm_calls=int(result.get("llm_calls") or 0),
                        cost_usd=float(cost),
                    )

                final_payload = {
                    "conversation_id": conversation_id,
                    "status": result.get("status"),
                    "route": result.get("route"),
                    "answer": answer,
                    "budget": budget,
                    "usage": result.get("usage") or {},
                    "trace": result.get("trace") or [],
                    "chunks": chunk_trails(result.get("chunks") or []),
                    "grade": result.get("grade"),
                    "ground_check": result.get("ground_check"),
                    "injection_flags": injection_flags,
                }
                if idempotency_cache_key and cache is not None:
                    await run_in_threadpool(
                        cache.set,
                        idempotency_cache_key,
                        final_payload,
                        services.config.redis.ttl_idempotency_s,
                    )
                logger.info(
                    "audit_chat",
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    status=result.get("status"),
                    tokens=(result.get("usage") or {}).get("total_tokens"),
                )
                turn_span.set_attribute(
                    "chat.status", str(result.get("status") or "error")
                )
                turn_span.set_attribute("chat.route", result.get("route"))
                turn_span.set_attribute("chat.answer_chars", len(answer))
                turn_span.set_attribute(
                    "chat.total_tokens", (result.get("usage") or {}).get("total_tokens")
                )
                yield sse_event("final", final_payload)
            except ClientDisconnected:
                # The client went away mid-answer. The turn is deliberately not persisted:
                # there is nobody to show it to, and recording a partial turn would make a
                # later retry look like a duplicate. The finally block still releases the
                # conversation lock, the tenant slot and the idempotency key.
                turn_span.set_attribute("chat.status", "client_disconnected")
                logger.info(
                    "chat_client_disconnected",
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                )
            except Exception as exc:
                turn_span.record_exception(exc)
                turn_span.set_attribute("chat.status", "error")
                logger.exception("chat_stream_failed", conversation_id=conversation_id)
                yield sse_event(
                    "error",
                    {"message": "the turn failed", "detail": type(exc).__name__},
                )
            finally:
                await turn_lock.release()
                services.release_tenant(tenant_id)
                await services.release_tenant_slot(tenant_id)
                await _release_idempotency_lock()
                SSE_ACTIVE.dec()

    return StreamingResponse(
        event_stream(), media_type="text/event-stream", headers=_SSE_HEADERS
    )


async def _replay_final(payload: dict):
    yield sse_event("final", {**payload, "replayed": True})
