"""Application service container.

Everything expensive (ONNX embedder, Chroma, LLM client, graph) is built lazily on the
first chat request so health checks and tests do not pay for it. Storage has a dev fallback
to SQLite; in production an unavailable database is a hard failure, as designed.
"""
from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from blackbox.api.store import ConversationRepository, SqlAlchemyConversationRepository
from blackbox.config.settings import AppConfig, Settings
from blackbox.logging_setup import get_logger

logger = get_logger(__name__)


class PersistenceUnavailable(RuntimeError):
    pass


@dataclass
class AppServices:
    settings: Settings
    config: AppConfig
    conversations: ConversationRepository
    retriever: Any | None = None
    llm: Any | None = None
    graph: Any | None = None
    entity_store: Any | None = None

    storage_ready: bool = False
    runtime_ready: bool = False
    storage_backend: str = ""

    ingest_lock: threading.Lock = field(default_factory=threading.Lock)
    eval_lock: threading.Lock = field(default_factory=threading.Lock)
    conversation_locks: dict[str, asyncio.Lock] = field(default_factory=dict)

    _storage_lock: threading.Lock = field(default_factory=threading.Lock)
    _runtime_lock: threading.Lock = field(default_factory=threading.Lock)

    # ---- storage ----

    def ensure_storage(self) -> None:
        if self.storage_ready:
            return
        with self._storage_lock:
            if self.storage_ready:
                return
            try:
                self.conversations.init()
                self.storage_ready = True
                if not self.storage_backend:
                    self.storage_backend = _backend_name(self.settings)
                return
            except Exception as exc:
                if self.settings.env == "prod":
                    logger.error("conversation_store_unavailable", error=str(exc))
                    raise PersistenceUnavailable(
                        "conversation store is unavailable"
                    ) from exc

            fallback_url = _sqlite_url("blackbox.db")
            logger.warning("conversation_store_fallback", fallback=fallback_url)
            try:
                self.conversations = SqlAlchemyConversationRepository(
                    fallback_url, memory_config=self.config.memory
                )
                self.conversations.init()
                self.storage_ready = True
                self.storage_backend = fallback_url
            except Exception as exc:
                logger.error("conversation_store_fallback_failed", error=str(exc))
                raise PersistenceUnavailable("conversation store is unavailable") from exc

    # ---- runtime ----

    def ensure_runtime(self) -> None:
        if self.runtime_ready:
            return
        with self._runtime_lock:
            if self.runtime_ready:
                return

            from blackbox.graph import build_graph
            from blackbox.ingestion.entity_extraction.store import SqlAlchemyEntityStore
            from blackbox.llm import build_llm_provider
            from blackbox.retrieval import build_default_retriever

            retriever = build_default_retriever(settings=self.settings)
            llm = build_llm_provider(self.settings)
            entity_store = self._build_entity_store(SqlAlchemyEntityStore)

            self.retriever = retriever
            self.llm = llm
            self.entity_store = entity_store
            self.graph = build_graph(
                retriever=retriever,
                llm=llm,
                entity_store=entity_store,
                settings=self.settings,
                config=self.config,
            )
            self.runtime_ready = True
            logger.info(
                "runtime_ready",
                corpus_chunks=getattr(retriever, "corpus_size", 0),
                llm=getattr(llm, "name", type(llm).__name__),
            )

    def _build_entity_store(self, store_cls):
        candidates = [self.settings.database_url, _sqlite_url("entities.db")]
        for url in candidates:
            if not url:
                continue
            try:
                store = store_cls(url)
                store.init()
                return store
            except Exception as exc:  # noqa: BLE001 - entity lookups degrade, chat does not
                logger.warning("entity_store_unavailable", url=url, error=str(exc))
        return None

    # ---- locks ----

    def lock_for(self, conversation_id: str) -> asyncio.Lock:
        lock = self.conversation_locks.get(conversation_id)
        if lock is None:
            lock = asyncio.Lock()
            self.conversation_locks[conversation_id] = lock
        return lock


def build_services(settings: Settings) -> AppServices:
    from blackbox.config.settings import get_config

    config = get_config()
    db_url = settings.database_url or _sqlite_url("blackbox.db")
    return AppServices(
        settings=settings,
        config=config,
        conversations=SqlAlchemyConversationRepository(db_url, memory_config=config.memory),
        storage_backend=_backend_name(settings),
    )


def _sqlite_url(filename: str) -> str:
    Path("data").mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{(Path('data') / filename).as_posix()}"


def _backend_name(settings: Settings) -> str:
    return "postgres" if (settings.database_url or "").startswith("postgres") else "sqlite"
