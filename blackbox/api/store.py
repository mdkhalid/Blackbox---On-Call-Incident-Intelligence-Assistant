"""Conversation persistence.

The DB is the source of truth: conversation memory, the last token allocation, and every
turn's trace. The repository is deliberately dumb (no graph knowledge) so the API can be
tested with SQLite while production uses Postgres.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import JSON, Column, Integer, String, Text, create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from blackbox.memory import ConversationMemory


class Base(DeclarativeBase):
    pass


class ConversationRow(Base):
    __tablename__ = "conversations"

    id = Column(String, primary_key=True)
    memory_json = Column(JSON, nullable=False)
    last_budget_json = Column(JSON, nullable=True)
    created_at = Column(String, nullable=False)
    updated_at = Column(String, nullable=False)


class TurnRow(Base):
    __tablename__ = "conversation_turns"

    id = Column(Integer, primary_key=True, autoincrement=True)
    conversation_id = Column(String, index=True, nullable=False)
    user_message = Column(Text, nullable=False)
    assistant_message = Column(Text, nullable=False)
    status = Column(String, nullable=False)
    trace_json = Column(JSON, nullable=False)
    budget_json = Column(JSON, nullable=False)
    chunks_json = Column(JSON, nullable=True)
    created_at = Column(String, nullable=False)


@dataclass
class ConversationRecord:
    id: str
    memory: ConversationMemory
    last_budget: dict
    created_at: str
    updated_at: str


@dataclass
class TurnRecord:
    id: int
    conversation_id: str
    user: str
    assistant: str
    status: str
    trace: list[dict]
    budget: dict
    chunks: list[dict]
    created_at: str


class ConversationRepository(Protocol):
    def init(self) -> None: ...
    def get(self, conversation_id: str) -> ConversationRecord | None: ...
    def get_or_create(self, conversation_id: str) -> ConversationRecord: ...
    def save_state(
        self, conversation_id: str, memory: ConversationMemory, last_budget: dict
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
    ) -> TurnRecord: ...
    def get_turns(self, conversation_id: str) -> list[TurnRecord]: ...
    def last_turn(self, conversation_id: str) -> TurnRecord | None: ...


class SqlAlchemyConversationRepository:
    def __init__(self, db_url: str, *, memory_config=None) -> None:
        connect_args = {"check_same_thread": False} if db_url.startswith("sqlite") else {}
        self.engine = create_engine(db_url, future=True, connect_args=connect_args)
        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.memory_config = memory_config

    def init(self) -> None:
        Base.metadata.create_all(self.engine)
        # Lightweight forward migration for stores created before chunk trails were added.
        if not inspect(self.engine).has_table("conversation_turns"):
            return
        columns = {column["name"] for column in inspect(self.engine).get_columns("conversation_turns")}
        if "chunks_json" not in columns:
            with self.engine.begin() as connection:
                connection.execute(
                    text("ALTER TABLE conversation_turns ADD COLUMN chunks_json JSON")
                )

    def _session(self) -> Session:
        return self.SessionLocal()

    # ---- reads ----

    def get(self, conversation_id: str) -> ConversationRecord | None:
        with self._session() as session:
            row = session.get(ConversationRow, conversation_id)
            return _record(row, self.memory_config) if row is not None else None

    def get_turns(self, conversation_id: str) -> list[TurnRecord]:
        with self._session() as session:
            rows = (
                session.query(TurnRow)
                .filter(TurnRow.conversation_id == conversation_id)
                .order_by(TurnRow.id)
                .all()
            )
            return [_turn(row) for row in rows]

    def last_turn(self, conversation_id: str) -> TurnRecord | None:
        with self._session() as session:
            row = (
                session.query(TurnRow)
                .filter(TurnRow.conversation_id == conversation_id)
                .order_by(TurnRow.id.desc())
                .first()
            )
            return _turn(row) if row is not None else None

    # ---- writes ----

    def get_or_create(self, conversation_id: str) -> ConversationRecord:
        with self._session() as session:
            row = session.get(ConversationRow, conversation_id)
            if row is None:
                now = _now()
                row = ConversationRow(
                    id=conversation_id,
                    memory_json=ConversationMemory(config=self.memory_config).to_dict()
                    if self.memory_config
                    else ConversationMemory().to_dict(),
                    last_budget_json={},
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                session.commit()
            return _record(row, self.memory_config)

    def save_state(
        self, conversation_id: str, memory: ConversationMemory, last_budget: dict
    ) -> None:
        with self._session() as session:
            row = session.get(ConversationRow, conversation_id)
            now = _now()
            if row is None:
                row = ConversationRow(
                    id=conversation_id,
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
    ) -> TurnRecord:
        with self._session() as session:
            now = _now()
            conversation = session.get(ConversationRow, conversation_id)
            if conversation is None:
                conversation = ConversationRow(
                    id=conversation_id,
                    memory_json=ConversationMemory().to_dict(),
                    last_budget_json=budget or {},
                    created_at=now,
                    updated_at=now,
                )
                session.add(conversation)
            conversation.updated_at = now
            row = TurnRow(
                conversation_id=conversation_id,
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
    ) -> TurnRecord:
        """Persist the updated memory and the finished turn in one transaction."""
        with self._session() as session:
            now = _now()
            conversation = session.get(ConversationRow, conversation_id)
            if conversation is None:
                conversation = ConversationRow(
                    id=conversation_id,
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
        memory=memory,
        last_budget=dict(row.last_budget_json or {}),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _turn(row: TurnRow) -> TurnRecord:
    return TurnRecord(
        id=row.id,
        conversation_id=row.conversation_id,
        user=row.user_message,
        assistant=row.assistant_message,
        status=row.status,
        trace=list(row.trace_json or []),
        budget=dict(row.budget_json or {}),
        chunks=list(row.chunks_json or []),
        created_at=row.created_at,
    )
