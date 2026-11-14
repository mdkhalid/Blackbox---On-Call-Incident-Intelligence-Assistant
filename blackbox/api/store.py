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

from blackbox.db import build_engine, install_rls_hook, recreate, schema_is_current
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
    # Where the conversation was written, and the region that owns it. Null for rows
    # created before multi-region; the resolver falls back to the default home region.
    region: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    home_region: Mapped[str | None] = mapped_column(String, nullable=True)


class TenantRegionRow(Base):
    """The tenant -> home-region mapping. The database, not a token, is authoritative."""

    __tablename__ = "tenant_regions"

    tenant_id: Mapped[str] = mapped_column(String, primary_key=True)
    home_region: Mapped[str] = mapped_column(String, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
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
    partition_month: Mapped[str | None] = mapped_column(String, nullable=True, index=True)


@dataclass
class ConversationRecord:
    id: str
    tenant_id: str
    memory: ConversationMemory
    last_budget: dict
    created_at: str
    updated_at: str
    region: str | None = None
    home_region: str | None = None


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
    def get_turns_page(
        self,
        conversation_id: str,
        *,
        tenant_id: str = DEFAULT_TENANT,
        limit: int = 100,
        offset: int = 0,
    ) -> list[TurnRecord]: ...
    def count_turns(
        self, conversation_id: str, *, tenant_id: str = DEFAULT_TENANT
    ) -> int: ...
    def turns_older_than(
        self, days: int, *, tenant_id: str | None = None
    ) -> list[TurnRecord]: ...
    def delete_turn_ids(self, ids: list[int]) -> int: ...
    def delete_tenant(self, tenant_id: str) -> int: ...

    def delete_older_than(self, days: int) -> int: ...
    def turns_for_tenant(
        self, tenant_id: str, *, days: int | None = None, limit: int = 10000
    ) -> list[TurnRecord]: ...
    def last_turn(
        self, conversation_id: str, *, tenant_id: str = DEFAULT_TENANT
    ) -> TurnRecord | None: ...


class SqlAlchemyConversationRepository:
    def __init__(
        self,
        db_url: str,
        *,
        memory_config=None,
        read_url: str | None = None,
        engine_kwargs: dict[str, Any] | None = None,
        rls_enabled: bool = False,
        region: str | None = None,
        home_region: str | None = None,
    ) -> None:
        engine_kwargs = engine_kwargs or {}
        self.engine = build_engine(db_url, **engine_kwargs)
        install_rls_hook(self.engine, enabled=rls_enabled)
        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False)
        # Reads may be served from a replica; writes never are. When no replica is
        # configured the reader *is* the writer, so the read path is unchanged.
        if read_url and read_url != db_url:
            self.read_engine = build_engine(read_url, **engine_kwargs)
            install_rls_hook(self.read_engine, enabled=rls_enabled)
            self.ReadSessionLocal = sessionmaker(
                bind=self.read_engine, expire_on_commit=False
            )
        else:
            self.read_engine = self.engine
            self.ReadSessionLocal = self.SessionLocal
        self.memory_config = memory_config
        self.region = region
        self.home_region_default = home_region

    def init(self) -> None:
        """Create tables for tests/local development.

        Production deployments run `alembic upgrade head` before starting the API; this
        method remains for fast test setup and a zero-friction local clone.
        """
        Base.metadata.create_all(self.engine)

    def schema_is_current(self) -> bool:
        """Whether the database's tables match the models.

        `create_all` creates missing tables but never alters an existing one, so a
        database written by an older version of the models keeps its old columns and
        every query against a new column fails with "no such column".
        """
        return schema_is_current(self.engine, Base.metadata)

    def recreate(self) -> None:
        """Drop and recreate the tables. Destroys the stored conversations."""
        recreate(self.engine, Base.metadata)

    def _session(self) -> Session:
        return self.SessionLocal()

    def _read_session(self) -> Session:
        return self.ReadSessionLocal()

    # ---- reads ----

    def get(
        self, conversation_id: str, *, tenant_id: str = DEFAULT_TENANT
    ) -> ConversationRecord | None:
        with self._read_session() as session:
            row = session.get(ConversationRow, conversation_id)
            if row is None or row.tenant_id != tenant_id:
                return None
            return _record(row, self.memory_config)

    def get_turns(
        self, conversation_id: str, *, tenant_id: str = DEFAULT_TENANT
    ) -> list[TurnRecord]:
        with self._read_session() as session:
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

    def get_turns_page(
        self,
        conversation_id: str,
        *,
        tenant_id: str = DEFAULT_TENANT,
        limit: int = 100,
        offset: int = 0,
    ) -> list[TurnRecord]:
        """One page of a conversation's turns, oldest first."""
        with self._read_session() as session:
            rows = (
                session.query(TurnRow)
                .filter(
                    TurnRow.conversation_id == conversation_id,
                    TurnRow.tenant_id == tenant_id,
                )
                .order_by(TurnRow.id)
                .offset(offset)
                .limit(limit)
                .all()
            )
            return [_turn(row) for row in rows]

    def count_turns(
        self, conversation_id: str, *, tenant_id: str = DEFAULT_TENANT
    ) -> int:
        with self._read_session() as session:
            return int(
                session.query(TurnRow)
                .filter(
                    TurnRow.conversation_id == conversation_id,
                    TurnRow.tenant_id == tenant_id,
                )
                .count()
            )

    def turns_older_than(
        self, days: int, *, tenant_id: str | None = None
    ) -> list[TurnRecord]:
        """Turns older than ``days``, oldest first, optionally for one tenant."""
        if days < 0:
            raise ValueError("days must be >= 0")
        from datetime import UTC, datetime, timedelta

        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        with self._read_session() as session:
            query = session.query(TurnRow).filter(TurnRow.created_at < cutoff)
            if tenant_id is not None:
                query = query.filter(TurnRow.tenant_id == tenant_id)
            rows = query.order_by(TurnRow.id).all()
            return [_turn(row) for row in rows]

    def delete_turn_ids(self, ids: list[int]) -> int:
        """Delete exactly the turns that were archived."""
        if not ids:
            return 0
        with self._session() as session:
            removed = (
                session.query(TurnRow)
                .filter(TurnRow.id.in_(ids))
                .delete(synchronize_session=False)
            )
            session.commit()
            return int(removed or 0)

    def delete_tenant(self, tenant_id: str) -> int:
        """Remove every conversation and turn owned by a tenant.

        Returns the number of conversations deleted. Index payloads and entity
        rows are store-wide today, so they are cleared separately by the caller.
        """
        if not tenant_id:
            raise ValueError("tenant_id must not be empty")
        with self._session() as session:
            session.query(TurnRow).filter(TurnRow.tenant_id == tenant_id).delete()
            convs = session.query(ConversationRow).filter(
                ConversationRow.tenant_id == tenant_id
            ).delete()
            session.query(TenantRegionRow).filter(
                TenantRegionRow.tenant_id == tenant_id
            ).delete()
            session.commit()
            return int(convs or 0)

    def delete_older_than(self, days: int) -> int:
        """Delete conversations and turns older than ``days``. Returns conversations removed."""
        if days < 0:
            raise ValueError("days must be >= 0")
        from datetime import UTC, datetime, timedelta

        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        with self._session() as session:
            session.query(TurnRow).filter(TurnRow.created_at < cutoff).delete()
            convs = session.query(ConversationRow).filter(
                ConversationRow.updated_at < cutoff
            ).delete()
            session.commit()
            return int(convs or 0)

    def turns_for_tenant(
        self, tenant_id: str, *, days: int | None = None, limit: int = 10000
    ) -> list[TurnRecord]:
        """Recent turns for one tenant, newest first. Used for cost aggregation."""
        if not tenant_id:
            raise ValueError("tenant_id must not be empty")
        with self._read_session() as session:
            query = session.query(TurnRow).filter(TurnRow.tenant_id == tenant_id)
            if days is not None:
                from datetime import UTC, datetime, timedelta

                cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
                query = query.filter(TurnRow.created_at >= cutoff)
            rows = query.order_by(TurnRow.id.desc()).limit(max(1, limit)).all()
            return [_turn(row) for row in rows]

    def last_turn(
        self, conversation_id: str, *, tenant_id: str = DEFAULT_TENANT
    ) -> TurnRecord | None:
        with self._read_session() as session:
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
                region=self.region,
                home_region=self.home_region_default,
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
                    region=self.region,
                    home_region=self.home_region_default,
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
                    region=self.region,
                    home_region=self.home_region_default,
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
                    region=self.region,
                    home_region=self.home_region_default,
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
                partition_month=now[:7],
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return _turn(row)

    # ---- tenant export / import (move procedure) ----

    def export_tenant(self, tenant_id: str) -> dict[str, Any]:
        """A portable copy of one tenant's conversations and turns.

        Used by the tenant-move procedure: the data is read here, written by
        `import_tenant` in the destination region, then the home-region mapping is
        flipped. Timestamps and ids are preserved so the moved conversation is
        indistinguishable from one written in place.
        """
        if not tenant_id:
            raise ValueError("tenant_id must not be empty")
        with self._read_session() as session:
            conversations = (
                session.query(ConversationRow)
                .filter(ConversationRow.tenant_id == tenant_id)
                .all()
            )
            conversation_ids = [row.id for row in conversations]
            turns = (
                session.query(TurnRow)
                .filter(
                    TurnRow.tenant_id == tenant_id,
                    TurnRow.conversation_id.in_(conversation_ids),
                )
                .order_by(TurnRow.id)
                .all()
                if conversation_ids
                else []
            )
            region_row = session.get(TenantRegionRow, tenant_id)
            return {
                "tenant_id": tenant_id,
                "conversations": [_conversation_row_dict(row) for row in conversations],
                "turns": [_turn_row_dict(row) for row in turns],
                "tenant_region": (
                    {
                        "home_region": region_row.home_region,
                        "status": region_row.status,
                        "updated_at": region_row.updated_at,
                    }
                    if region_row is not None
                    else None
                ),
            }

    def import_tenant(self, data: dict[str, Any], *, home_region: str | None = None) -> dict[str, int]:
        """Write an exported tenant into this database.

        Idempotent per conversation: an existing id is left alone, so a retried move
        does not duplicate rows. Returns the number of rows written.
        """
        tenant_id = str(data.get("tenant_id") or "")
        if not tenant_id:
            raise ValueError("export is missing tenant_id")
        conversations = data.get("conversations") or []
        turns = data.get("turns") or []
        written_convs = 0
        written_turns = 0
        with self._session() as session:
            existing = {
                row.id
                for row in session.query(ConversationRow)
                .filter(ConversationRow.tenant_id == tenant_id)
                .all()
            }
            for payload in conversations:
                if payload["id"] in existing:
                    continue
                session.add(ConversationRow(**payload))
                written_convs += 1
            existing_turns = {
                row.id
                for row in session.query(TurnRow)
                .filter(TurnRow.tenant_id == tenant_id)
                .all()
            }
            for payload in turns:
                if payload["id"] in existing_turns:
                    continue
                session.add(TurnRow(**payload))
                written_turns += 1
            session.commit()
        region = home_region or (data.get("tenant_region") or {}).get("home_region")
        if region:
            self.set_home_region(tenant_id, region, status="active")
        return {"conversations": written_convs, "turns": written_turns}

    # ---- tenant -> region mapping ----

    def home_region(self, tenant_id: str) -> str | None:
        """The persisted home region for a tenant, or None when unmapped."""
        with self._read_session() as session:
            row = session.get(TenantRegionRow, tenant_id)
            return row.home_region if row is not None else None

    def tenant_region_status(self, tenant_id: str) -> str | None:
        with self._read_session() as session:
            row = session.get(TenantRegionRow, tenant_id)
            return row.status if row is not None else None

    def set_home_region(
        self, tenant_id: str, region: str, *, status: str = "active"
    ) -> None:
        """Pin a tenant to a home region (used on onboarding, failover and move)."""
        if not tenant_id:
            raise ValueError("tenant_id must not be empty")
        if not region:
            raise ValueError("region must not be empty")
        with self._session() as session:
            row = session.get(TenantRegionRow, tenant_id)
            now = _now()
            if row is None:
                session.add(
                    TenantRegionRow(
                        tenant_id=tenant_id,
                        home_region=region,
                        status=status,
                        updated_at=now,
                    )
                )
            else:
                row.home_region = region
                row.status = status
                row.updated_at = now
            session.commit()


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
        region=row.region,
        home_region=row.home_region,
    )


def _conversation_row_dict(row: ConversationRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "tenant_id": row.tenant_id,
        "memory_json": row.memory_json or {},
        "last_budget_json": row.last_budget_json or {},
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "region": row.region,
        "home_region": row.home_region,
    }


def _turn_row_dict(row: TurnRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "conversation_id": row.conversation_id,
        "tenant_id": row.tenant_id,
        "user_message": row.user_message,
        "assistant_message": row.assistant_message,
        "status": row.status,
        "trace_json": row.trace_json or [],
        "budget_json": row.budget_json or {},
        "chunks_json": row.chunks_json or [],
        "created_at": row.created_at,
        "partition_month": row.partition_month,
    }


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
