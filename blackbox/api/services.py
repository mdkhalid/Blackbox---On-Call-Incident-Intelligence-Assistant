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
    cache_backend: Any | None = None
    lock_engine: Any | None = None
    rate_limiter: Any | None = None
    trace_archive: Any | None = None

    conversation_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    job_queues: dict[str, Any] = field(default_factory=dict)

    _storage_lock: threading.Lock = field(default_factory=threading.Lock)
    _runtime_lock: threading.Lock = field(default_factory=threading.Lock)
    _lock_engine_lock: threading.Lock = field(default_factory=threading.Lock)
    _rate_limiter_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _tenant_counts: dict[str, int] = field(default_factory=dict)
    _tenant_counts_lock: threading.Lock = field(default_factory=threading.Lock)

    # ---- storage ----

    def ensure_storage(self) -> None:
        if self.storage_ready:
            return
        with self._storage_lock:
            if self.storage_ready:
                return
            try:
                self.conversations.init()
                self._recreate_if_stale(self.conversations)
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
                self._recreate_if_stale(self.conversations)
                self.storage_ready = True
                self.storage_backend = fallback_url
            except Exception as exc:
                logger.error("conversation_store_fallback_failed", error=str(exc))
                raise PersistenceUnavailable("conversation store is unavailable") from exc

    def _recreate_if_stale(self, repository) -> None:
        """Recreate a stale local development database.

        `create_all` creates missing tables but never alters an existing one, so a
        database written by an older version of the models keeps its old columns and
        every query against a new column fails with "no such column". A developer
        whose local database predates a schema change would otherwise find the app
        failing at the first query, with nothing in the logs to explain it.

        The local development database holds scratch conversations, so it is recreated
        rather than left broken. This never applies to Postgres, and never in
        production, where Alembic owns the schema.
        """
        if self.settings.env == "prod":
            return
        url = str(repository.engine.url)
        if not url.startswith("sqlite"):
            return
        if repository.schema_is_current():
            return
        logger.warning(
            "conversation_store_schema_stale",
            url=url,
            action="recreated",
        )
        repository.recreate()

    # ---- runtime ----

    def ensure_runtime(self) -> None:
        if self.runtime_ready:
            return
        with self._runtime_lock:
            if self.runtime_ready:
                return

            from blackbox.cache import CachedEmbeddingProvider, build_cache
            from blackbox.graph import build_graph
            from blackbox.ingestion.entity_extraction.store import SqlAlchemyEntityStore
            from blackbox.llm import build_llm_provider
            from blackbox.llm.cached import CachedLLMProvider
            from blackbox.llm.resilient import ResilientLLMProvider
            from blackbox.retrieval import build_default_retriever

            cache = build_cache(self.settings, self.config)
            retrieval_config = self.config.retrieval
            if self.settings.embedding_provider_override:
                retrieval_config = retrieval_config.model_copy(
                    update={
                        "embedding_provider": self.settings.embedding_provider_override
                    }
                )
            retriever = build_default_retriever(
                settings=self.settings, retrieval_config=retrieval_config
            )
            if cache is not None:
                retriever.vectorstore.embedding_provider = CachedEmbeddingProvider(
                    retriever.vectorstore.embedding_provider,
                    cache,
                    ttl_s=self.config.redis.ttl_embedding_s,
                )
                retriever.cache = cache
                retriever.cache_ttl_s = self.config.redis.ttl_retrieval_s

            llm = build_llm_provider(self.settings)
            llm = ResilientLLMProvider(
                llm,
                timeout_s=self.settings.llm_timeout_s,
                max_retries=self.settings.llm_max_retries,
                max_concurrency=self.settings.llm_max_concurrency,
                failure_threshold=self.settings.llm_circuit_threshold,
                reset_s=self.settings.llm_circuit_reset_s,
            )
            if (
                cache is not None
                and self.settings.env != "prod"
                and self.config.redis.ttl_llm_response_s > 0
            ):
                llm = CachedLLMProvider(
                    llm, cache, ttl_s=self.config.redis.ttl_llm_response_s
                )
            entity_store = self._build_entity_store(SqlAlchemyEntityStore)

            self.retriever = retriever
            self.llm = llm
            self.cache_backend = cache
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

    async def acquire_turn_lock(self, conversation_id: str, tenant_id: str = "default"):
        from blackbox.api.locks import (
            ConversationBusy,
            LocalTurnLock,
            acquire_postgres_lock,
        )

        lock_key = f"{tenant_id}:{conversation_id}"
        if self.storage_backend == "postgres" and hasattr(self.conversations, "engine"):
            if self.lock_engine is None:
                with self._lock_engine_lock:
                    if self.lock_engine is None:
                        self.lock_engine = build_lock_engine(self.settings.database_url)
            lock = await acquire_postgres_lock(self.lock_engine, lock_key)
            if lock is None:
                raise ConversationBusy("conversation already has an active turn")
            return lock

        local = self.lock_for(lock_key)
        if local.locked():
            raise ConversationBusy("conversation already has an active turn")
        await local.acquire()
        return LocalTurnLock(
            local, on_release=lambda: self.release_lock(lock_key, local)
        )

    def lock_for(self, conversation_id: str) -> asyncio.Lock:
        lock = self.conversation_locks.get(conversation_id)
        if lock is None:
            lock = asyncio.Lock()
            self.conversation_locks[conversation_id] = lock
        return lock

    def release_lock(self, key: str, lock: asyncio.Lock) -> None:
        """Drop an idle turn lock from the registry.

        The registry must not grow without bound: a long-lived process serving
        thousands of distinct conversations a day would otherwise hold one lock object
        per conversation for the lifetime of the process.

        Only the lock currently registered for this key is dropped, and only while it
        is unlocked, so a conversation that is still busy keeps its lock and mutual
        exclusion is preserved. The local lock is a try-lock - a second caller is
        rejected with 409 rather than queued - so an unlocked lock has no waiters.
        """
        if self.conversation_locks.get(key) is lock and not lock.locked():
            self.conversation_locks.pop(key, None)

    async def ensure_rate_limiter(self):
        """Redis-backed limiter when reachable, local fallback otherwise."""
        if self.rate_limiter is not None:
            return self.rate_limiter
        async with self._rate_limiter_lock:
            if self.rate_limiter is not None:
                return self.rate_limiter
            from blackbox.api.rate_limit import LocalRateLimiter, build_rate_limiter

            fallback = LocalRateLimiter()
            self.rate_limiter = await build_rate_limiter(
                self.settings, self.config, fallback
            )
            return self.rate_limiter

    def try_acquire_tenant(self, tenant_id: str) -> bool:
        """Per-process admission control for concurrent turns of one tenant."""
        limit = self.config.guardrails.max_concurrent_turns_per_tenant
        with self._tenant_counts_lock:
            count = self._tenant_counts.get(tenant_id, 0)
            if count >= limit:
                return False
            self._tenant_counts[tenant_id] = count + 1
            return True

    def release_tenant(self, tenant_id: str) -> None:
        with self._tenant_counts_lock:
            count = self._tenant_counts.get(tenant_id, 0)
            if count <= 1:
                self._tenant_counts.pop(tenant_id, None)
            else:
                self._tenant_counts[tenant_id] = count - 1

    # ---- background jobs ----

    def job_queue(self, kind: str):
        """The queue for one kind of operational job."""
        from blackbox.api.jobs import JobQueue

        queue = self.job_queues.get(kind)
        if queue is None:
            queue = JobQueue(kind)
            self.job_queues[kind] = queue
        return queue

    def submit_job(self, kind: str, fn, *, tenant_id: str, subject: str):
        """Queue a background job and return it immediately.

        The caller gets a job id and polls the status endpoint; the work happens on a
        worker instead of inside the request.
        """
        return self.job_queue(kind).submit(fn, tenant_id=tenant_id, subject=subject)

    def find_job(self, job_id: str, *, kind: str | None = None):
        """Find a job by id, optionally restricted to one kind."""
        for queue in self.job_queues.values():
            job = queue.get(job_id)
            if job is not None and (kind is None or job.kind == kind):
                return queue, job
        return None

    def stop_jobs(self) -> None:
        """Stop the background job workers."""
        for queue in self.job_queues.values():
            queue.stop()

    # ---- trace archive ----

    def get_trace_archive(self):
        """The configured cold-storage backend, built once per process."""
        if self.trace_archive is None:
            from blackbox.api.trace_archive import build_trace_archive

            self.trace_archive = build_trace_archive(self.settings)
        return self.trace_archive

    def close(self) -> None:
        """Release process-owned resources during shutdown."""
        self.stop_jobs()
        archive = self.trace_archive
        if archive is not None:
            archive.close()


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


def build_lock_engine(db_url: str | None):
    """Dedicated engine for advisory locks: no pooling, no connection held from the app pool."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    if not db_url:
        raise ValueError("a database URL is required for Postgres advisory locks")
    return create_engine(db_url, poolclass=NullPool, future=True)
