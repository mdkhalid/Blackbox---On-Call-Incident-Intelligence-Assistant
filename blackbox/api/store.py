"""Conversation persistence with tenant isolation.

The DB is the source of truth: conversation memory, the last token allocation, and every
turn's trace. Conversation IDs are globally unique today; every read/write is additionally
scoped by tenant, and cross-tenant access fails closed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import JSON, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from blackbox.db import build_engine
from blackbox.memory import ConversationMemory

DEFAULT_TENANT = "default"


class ConversationTenantMismatch(LookupError):
    """The conversation id exists but belongs to another tenant."""


class Base(DeclarativeBase):
    pass


class ConversationRow(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String, nullable=False, default=DEFAULT_TENANT, index=True
    )
    memory_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    last_budget_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class TurnRow(Base):
    __tablename__ = "conversation_turns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    tenant_id: Mapped[str] = mapped_column(
        String, nullable=False, default=DEFAULT_TENANT, index=True
    )
    user_message: Mapped[str] = mapped_column(Text, nullable=False)
    assistant_message: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    trace_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    budget_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    chunks_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False)


@dataclass
class ConversationRecord:
    id: str
    tenant_id: str
    memory: ConversationMemory
    last_budget: dict
    created_at: str
    updated_at: str


@dataclass
class TurnRecord:
    id: int
    conversation_id: str
    tenant_id: str
    user: str
    assistant: str
    status: str
    trace: list[dict]
    budget: dict
    chunks: list[dict]
    created_at: str


class ConversationRepository(Protocol):
    def init(self) -> None: ...
    def get(
        self, conversation_id: str, *, tenant_id: str = DEFAULT_TENANT
    ) -> ConversationRecord | None: ...
    def get_or_create(
        self, conversation_id: str, *, tenant_id: str = DEFAULT_TENANT
    ) -> ConversationRecord: ...
    def save_state(
        self,
        conversation_id: str,
        memory: ConversationMemory,
        last_budget: dict,
        *,
        tenant_id: str = DEFAULT_TENANT,
    ) -> None: ...
    def append_turn(
        self,
        conversation_id: str,
        user: str,
        assistant: str,
        status: str,
        trace: list[dict],
        budget: dict,
        chunks: list[dict] | None = None,
        *,
        tenant_id: str = DEFAULT_TENANT,
    ) -> TurnRecord: ...
    def record_turn(
        self,
        conversation_id: str,
        memory: ConversationMemory,
        last_budget: dict,
        user: str,
        assistant: str,
        status: str,
        trace: list[dict],
        chunks: list[dict] | None = None,
        *,
        tenant_id: str = DEFAULT_TENANT,
    ) -> TurnRecord: ...
    def get_turns(
        self, conversation_id: str, *, tenant_id: str = DEFAULT_TENANT
    ) -> list[TurnRecord]: ...
    def last_turn(
        self, conversation_id: str, *, tenant_id: str = DEFAULT_TENANT
    ) -> TurnRecord | None: ...


class SqlAlchemyConversationRepository:
    def __init__(self, db_url: str, *, memory_config=None) -> None:
        self.engine = build_engine(db_url)
        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.memory_config = memory_config

    def init(self) -> None:
        """Create tables for tests/local development.

        Production deployments run `alembic upgrade head` before starting the API; this
        method remains for fast test setup and a zero-friction local clone.
        """
        Base.metadata.create_all(self.engine)

    def _session(self) -> Session:
        return self.SessionLocal()

    # ---- reads ----

    def get(
        self, conversation_id: str, *, tenant_id: str = DEFAULT_TENANT
    ) -> ConversationRecord | None:
        with self._session() as session:
            row = session.get(ConversationRow, conversation_id)
            if row is None or row.tenant_id != tenant_id:
                return None
            return _record(row, self.memory_config)

    def get_turns(
        self, conversation_id: str, *, tenant_id: str = DEFAULT_TENANT
    ) -> list[TurnRecord]:
        with self._session() as session:
            rows = (
                session.query(TurnRow)
                .filter(
                    TurnRow.conversation_id == conversation_id,
                    TurnRow.tenant_id == tenant_id,
                )
                .order_by(TurnRow.id)
                .all()
            )
            return [_turn(row) for row in rows]

    def last_turn(
        self, conversation_id: str, *, tenant_id: str = DEFAULT_TENANT
    ) -> TurnRecord | None:
        with self._session() as session:
            row = (
                session.query(TurnRow)
                .filter(
                    TurnRow.conversation_id == conversation_id,
                    TurnRow.tenant_id == tenant_id,
                )
                .order_by(TurnRow.id.desc())
                .first()
            )
            return _turn(row) if row is not None else None

    # ---- writes ----

    def get_or_create(
        self, conversation_id: str, *, tenant_id: str = DEFAULT_TENANT
    ) -> ConversationRecord:
        with self._session() as session:
            row = session.get(ConversationRow, conversation_id)
            if row is not None:
                if row.tenant_id != tenant_id:
                    raise ConversationTenantMismatch(conversation_id)
                return _record(row, self.memory_config)

            now = _now()
            row = ConversationRow(
                id=conversation_id,
                tenant_id=tenant_id,
                memory_json=(
                    ConversationMemory(config=self.memory_config).to_dict()
                    if self.memory_config
                    else ConversationMemory().to_dict()
                ),
                last_budget_json={},
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.commit()
            return _record(row, self.memory_config)

    def _owned_conversation(self, session: Session, conversation_id: str, tenant_id: str):
        row = session.get(ConversationRow, conversation_id)
        if row is not None and row.tenant_id != tenant_id:
            raise ConversationTenantMismatch(conversation_id)
        return row

    def save_state(
        self,
        conversation_id: str,
        memory: ConversationMemory,
        last_budget: dict,
        *,
        tenant_id: str = DEFAULT_TENANT,
    ) -> None:
        with self._session() as session:
            row = self._owned_conversation(session, conversation_id, tenant_id)
            now = _now()
            if row is None:
                row = ConversationRow(
                    id=conversation_id,
                    tenant_id=tenant_id,
                    memory_json=memory.to_dict(),
                    last_budget_json=last_budget or {},
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
            else:
                row.memory_json = memory.to_dict()
                row.last_budget_json = last_budget or {}
                row.updated_at = now
            session.commit()

    def append_turn(
        self,
        conversation_id: str,
        user: str,
        assistant: str,
        status: str,
        trace: list[dict],
        budget: dict,
        chunks: list[dict] | None = None,
        *,
        tenant_id: str = DEFAULT_TENANT,
    ) -> TurnRecord:
        with self._session() as session:
            now = _now()
            conversation = self._owned_conversation(session, conversation_id, tenant_id)
            if conversation is None:
                conversation = ConversationRow(
                    id=conversation_id,
                    tenant_id=tenant_id,
                    memory_json=ConversationMemory().to_dict(),
                    last_budget_json=budget or {},
                    created_at=now,
                    updated_at=now,
                )
                session.add(conversation)
            conversation.updated_at = now
            row = TurnRow(
                conversation_id=conversation_id,
                tenant_id=tenant_id,
                user_message=user,
                assistant_message=assistant,
                status=status,
                trace_json=trace or [],
                budget_json=budget or {},
                chunks_json=chunks or [],
                created_at=now,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return _turn(row)

    def record_turn(
        self,
        conversation_id: str,
        memory: ConversationMemory,
        last_budget: dict,
        user: str,
        assistant: str,
        status: str,
        trace: list[dict],
        chunks: list[dict] | None = None,
        *,
        tenant_id: str = DEFAULT_TENANT,
    ) -> TurnRecord:
        """Persist the updated memory and the finished turn in one transaction."""
        with self._session() as session:
            now = _now()
            conversation = self._owned_conversation(session, conversation_id, tenant_id)
            if conversation is None:
                conversation = ConversationRow(
                    id=conversation_id,
                    tenant_id=tenant_id,
                    memory_json=memory.to_dict(),
                    last_budget_json=last_budget or {},
                    created_at=now,
                    updated_at=now,
                )
                session.add(conversation)
            else:
                conversation.memory_json = memory.to_dict()
                conversation.last_budget_json = last_budget or {}
                conversation.updated_at = now

            row = TurnRow(
                conversation_id=conversation_id,
                tenant_id=tenant_id,
                user_message=user,
                assistant_message=assistant,
                status=status,
                trace_json=trace or [],
                budget_json=last_budget or {},
                chunks_json=chunks or [],
                created_at=now,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return _turn(row)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _record(row: ConversationRow, memory_config=None) -> ConversationRecord:
    if memory_config is not None:
        memory = ConversationMemory.from_dict(row.memory_json or {}, config=memory_config)
    else:
        memory = ConversationMemory.from_dict(row.memory_json or {})
    return ConversationRecord(
        id=row.id,
        tenant_id=row.tenant_id or DEFAULT_TENANT,
        memory=memory,
        last_budget=dict(row.last_budget_json or {}),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _turn(row: TurnRow) -> TurnRecord:
    return TurnRecord(
        id=row.id,
        conversation_id=row.conversation_id,
        tenant_id=row.tenant_id or DEFAULT_TENANT,
        user=row.user_message,
        assistant=row.assistant_message,
        status=row.status,
        trace=list(row.trace_json or []),
        budget=dict(row.budget_json or {}),
        chunks=list(row.chunks_json or []),
        created_at=row.created_at,
    )
