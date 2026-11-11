"""The hybrid retriever: dense + sparse in parallel, fused, then re-ranked.

This is a real LangChain `BaseRetriever`, so it composes with LCEL chains and LangGraph
nodes (`retriever | prompt | llm`, or as a node's retrieval step) instead of being a
standalone function the rest of the system has to special-case.

Stage order (each stage is timed and recorded for the trace viewer):

    dense ─┐
           ├─ fuse ─ rerank ─ top_k
    sparse ┘

Filtering is compiled to a Chroma `where` clause for dense and applied to the candidate
set for sparse, so nothing is ever retrieved unfiltered and trimmed afterwards.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

from langchain_core.callbacks import (
    AsyncCallbackManagerForRetrieverRun,
    CallbackManagerForRetrieverRun,
)
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from pydantic import ConfigDict, Field, PrivateAttr

from blackbox.config.settings import RetrievalConfig
from blackbox.ingestion.vectorstore import ChromaVectorStore
from blackbox.logging_setup import get_logger
from blackbox.retrieval.dense import DenseRetriever
from blackbox.retrieval.filters import MetadataFilter, parse_filter
from blackbox.retrieval.fusion import FusionConfig, fuse
from blackbox.retrieval.models import RetrievalResult, RetrievedChunk
from blackbox.retrieval.rerank import build_reranker
from blackbox.retrieval.sparse import SparseRetriever
from blackbox.retrieval.timeout import Deadline

logger = get_logger(__name__)


@dataclass(slots=True)
class _RetrievalPlan:
    """The resolved settings of one retrieval, shared by the sync and async paths."""

    query: str
    k: int
    pool: int
    md_filter: MetadataFilter | None
    wants_rerank: bool
    dense_only: bool
    sparse_only: bool
    cache_key: str | None
    deadline: Deadline


class HybridRetriever(BaseRetriever):
    """Dense + BM25 retrieval with fusion and an optional rerank pass."""

    vectorstore: ChromaVectorStore = Field(exclude=True, repr=False)
    model_config = ConfigDict(arbitrary_types_allowed=True)

    config: RetrievalConfig = Field(default_factory=RetrievalConfig)
    default_filter: MetadataFilter | None = None
    reranker: Any = None
    top_k: int | None = None
    rerank_enabled: bool | None = None
    cache: Any = Field(default=None, exclude=True, repr=False)
    cache_ttl_s: int = 0

    _dense: DenseRetriever | None = PrivateAttr(default=None)
    _sparse: SparseRetriever | None = PrivateAttr(default=None)

    def model_post_init(self, /, __context: Any) -> None:
        self._dense = DenseRetriever(self.vectorstore)
        self._sparse = SparseRetriever(self.vectorstore)
        if self.reranker is None:
            corpus_texts: list[str] = []
            if self.config.rerank_provider in ("lexical", "auto"):
                # The lexical reranker needs corpus IDF, and the BM25 index has already
                # loaded the same documents, so this fetch is not extra work in practice.
                corpus_texts = list(self.vectorstore.get_all().get("documents") or [])
            self.reranker = build_reranker(
                self.config.rerank_provider,
                model_name="cross-encoder/ms-marco-MiniLM-L-6-v2",
                corpus_texts=corpus_texts,
                warn=lambda msg: logger.info("rerank_fallback", reason=msg),
            )

    @property
    def dense(self) -> DenseRetriever:
        if self._dense is None:  # pragma: no cover - set in model_post_init
            raise RuntimeError("the dense retriever is not initialised")
        return self._dense

    @property
    def sparse(self) -> SparseRetriever:
        if self._sparse is None:  # pragma: no cover - set in model_post_init
            raise RuntimeError("the sparse retriever is not initialised")
        return self._sparse

    # ---- index management ----

    def invalidate(self) -> None:
        """Drop cached corpus-derived state; call after ingestion changes the collection.

        Both the BM25 index and the lexical reranker's IDF table are built from the corpus,
        so both must be rebuilt or retrieval would score against a stale vocabulary.
        """
        if self._sparse is not None:
            self._sparse.invalidate()
        if type(self.reranker).__name__ == "LexicalReranker":
            corpus_texts = list(self.vectorstore.get_all().get("documents") or [])
            self.reranker = type(self.reranker).from_corpus(corpus_texts)
        if self.cache is not None:
            self.cache.clear_prefix("ret:")

    @property
    def corpus_size(self) -> int:
        return self.sparse.size

    # ---- core retrieval ----

    def retrieve(
        self,
        query: str,
        *,
        top_k: int | None = None,
        filter: MetadataFilter | dict[str, Any] | None = None,
        use_rerank: bool | None = None,
        dense_only: bool = False,
        sparse_only: bool = False,
    ) -> list[RetrievedChunk]:
        """Run the full pipeline and return chunks carrying their whole score trail.

        `dense_only` / `sparse_only` exist so the eval harness compares the four
        strategies through identical code rather than reimplementing them.

        Callers that need this call's timings use `retrieve_detailed` instead.
        """
        return self.retrieve_detailed(
            query,
            top_k=top_k,
            filter=filter,
            use_rerank=use_rerank,
            dense_only=dense_only,
            sparse_only=sparse_only,
        ).chunks

    def retrieve_detailed(
        self,
        query: str,
        *,
        top_k: int | None = None,
        filter: MetadataFilter | dict[str, Any] | None = None,
        use_rerank: bool | None = None,
        dense_only: bool = False,
        sparse_only: bool = False,
    ) -> RetrievalResult:
        """Run the full pipeline and return the chunks with this call's own timings.

        The timings and counts belong to this call. They are returned rather than
        published on the instance because the retriever is a shared singleton: instance
        state would let concurrent requests overwrite and then read each other's
        latency numbers, which is precisely what the trace viewer must not show.
        """
        plan = self._plan(
            query,
            top_k=top_k,
            filter=filter,
            use_rerank=use_rerank,
            dense_only=dense_only,
            sparse_only=sparse_only,
        )

        if plan.cache_key:
            started = time.perf_counter()
            cached = self.cache.get(plan.cache_key)
            if isinstance(cached, list):
                results = [RetrievedChunk.from_cache_dict(item) for item in cached]
                elapsed = round((time.perf_counter() - started) * 1000, 2)
                logger.info(
                    "retrieval_cache_hit", query=query[:120], returned=len(results)
                )
                return RetrievalResult(
                    chunks=results,
                    timings={"cache_ms": elapsed, "total_ms": elapsed},
                    counts={
                        "dense": 0,
                        "sparse": 0,
                        "fused": 0,
                        "returned": len(results),
                    },
                    cached=True,
                )

        # Build corpus-derived indexes before the deadline starts. The BM25 index and the
        # embedding model are one-time startup costs, not per-request work; charging them to
        # the first query would make a healthy cold request look like a timeout. The deadline
        # must be constructed *after* this, or its clock starts before the warm-up finishes.
        self.sparse.warm_up()

        dense_hits, dense_ms = self._run_leg(plan, "dense")
        sparse_hits, sparse_ms = self._run_leg(plan, "sparse")

        result = self._finalize(
            plan,
            dense_hits=dense_hits,
            sparse_hits=sparse_hits,
            timings={"dense_ms": dense_ms, "sparse_ms": sparse_ms},
        )
        if plan.cache_key:
            self.cache.set(
                plan.cache_key,
                [chunk.to_cache_dict() for chunk in result.chunks],
                self.cache_ttl_s,
            )
        return result

    async def aretrieve_detailed(
        self,
        query: str,
        *,
        top_k: int | None = None,
        filter: MetadataFilter | dict[str, Any] | None = None,
        use_rerank: bool | None = None,
        dense_only: bool = False,
        sparse_only: bool = False,
    ) -> RetrievalResult:
        """Async retrieval with the dense and sparse legs running concurrently.

        The synchronous path runs the legs one after the other, so a request pays for
        both. Here each leg is dispatched to a worker thread and awaited together, so
        the request pays for the slower of the two rather than the sum of both.
        """
        plan = self._plan(
            query,
            top_k=top_k,
            filter=filter,
            use_rerank=use_rerank,
            dense_only=dense_only,
            sparse_only=sparse_only,
        )

        if plan.cache_key:
            started = time.perf_counter()
            cached = await asyncio.to_thread(self.cache.get, plan.cache_key)
            if isinstance(cached, list):
                results = [RetrievedChunk.from_cache_dict(item) for item in cached]
                elapsed = round((time.perf_counter() - started) * 1000, 2)
                logger.info(
                    "retrieval_cache_hit", query=query[:120], returned=len(results)
                )
                return RetrievalResult(
                    chunks=results,
                    timings={"cache_ms": elapsed, "total_ms": elapsed},
                    counts={
                        "dense": 0,
                        "sparse": 0,
                        "fused": 0,
                        "returned": len(results),
                    },
                    cached=True,
                )

        await asyncio.to_thread(self.sparse.warm_up)

        dense_task = asyncio.to_thread(self._run_leg, plan, "dense")
        sparse_task = asyncio.to_thread(self._run_leg, plan, "sparse")
        (dense_hits, dense_ms), (sparse_hits, sparse_ms) = await asyncio.gather(
            dense_task, sparse_task
        )

        result = self._finalize(
            plan,
            dense_hits=dense_hits,
            sparse_hits=sparse_hits,
            timings={"dense_ms": dense_ms, "sparse_ms": sparse_ms},
        )
        if plan.cache_key:
            await asyncio.to_thread(
                self.cache.set,
                plan.cache_key,
                [chunk.to_cache_dict() for chunk in result.chunks],
                self.cache_ttl_s,
            )
        return result

    def _plan(
        self,
        query: str,
        *,
        top_k: int | None,
        filter: MetadataFilter | dict[str, Any] | None,
        use_rerank: bool | None,
        dense_only: bool,
        sparse_only: bool,
    ) -> _RetrievalPlan:
        """Resolve the settings shared by the sync and async retrieval paths."""
        k = top_k or self.top_k or self.config.top_k
        md_filter = self._coerce_filter(filter)
        wants_rerank = (
            self.rerank_enabled if self.rerank_enabled is not None else self.config.rerank
        )
        if use_rerank is not None:
            wants_rerank = use_rerank
        return _RetrievalPlan(
            query=query,
            k=k,
            pool=max(self.config.candidate_pool, k),
            md_filter=md_filter,
            wants_rerank=wants_rerank,
            dense_only=dense_only,
            sparse_only=sparse_only,
            cache_key=self._cache_key(
                query=query,
                top_k=k,
                md_filter=md_filter,
                use_rerank=wants_rerank,
                dense_only=dense_only,
                sparse_only=sparse_only,
            ),
            deadline=Deadline(
                self.config.retrieval_timeout_ms, operation="hybrid_retrieve"
            ),
        )

    def _run_leg(
        self, plan: _RetrievalPlan, leg: str
    ) -> tuple[list[RetrievedChunk], float]:
        """Run one retrieval leg and report how long it took."""
        started = time.perf_counter()
        if leg == "dense":
            hits = self._dense_leg(plan)
        else:
            hits = self._sparse_leg(plan)
        return hits, round((time.perf_counter() - started) * 1000, 2)

    def _dense_leg(self, plan: _RetrievalPlan) -> list[RetrievedChunk]:
        if plan.sparse_only:
            return []
        return plan.deadline.run(
            "dense",
            lambda: self.dense.search(
                plan.query, top_k=plan.pool, filter=plan.md_filter
            ),
        )

    def _sparse_leg(self, plan: _RetrievalPlan) -> list[RetrievedChunk]:
        if plan.dense_only:
            return []
        return plan.deadline.run(
            "sparse",
            lambda: self.sparse.search(
                plan.query, top_k=plan.pool, filter=plan.md_filter
            ),
        )

    def _finalize(
        self,
        plan: _RetrievalPlan,
        *,
        dense_hits: list[RetrievedChunk],
        sparse_hits: list[RetrievedChunk],
        timings: dict[str, float],
    ) -> RetrievalResult:
        """Fuse, re-rank and score-floor the two legs into the final result."""
        timings = dict(timings)
        pool = plan.pool
        k = plan.k

        t0 = time.perf_counter()
        if plan.dense_only:
            fused = _rank_single(dense_hits, "dense", pool)
        elif plan.sparse_only:
            fused = _rank_single(sparse_hits, "sparse", pool)
        else:
            fused = fuse(
                dense_hits,
                sparse_hits,
                FusionConfig(
                    method=self.config.fusion_method,
                    k=self.config.fusion_k,
                    dense_weight=self.config.dense_weight,
                    sparse_weight=self.config.sparse_weight,
                ),
                top_k=pool,
            )
        timings["fuse_ms"] = round((time.perf_counter() - t0) * 1000, 2)

        t0 = time.perf_counter()
        if plan.wants_rerank and fused and self.reranker is not None:
            candidates = fused[: max(self.config.rerank_candidates, k)]
            results = plan.deadline.run(
                "rerank",
                lambda: self.reranker.rerank(
                    plan.query, candidates, top_k=k, filter=plan.md_filter
                ),
            )
        else:
            results = fused[:k]
        timings["rerank_ms"] = round((time.perf_counter() - t0) * 1000, 2)

        results = self._apply_score_floor(
            results, reranked=bool(plan.wants_rerank and self.reranker)
        )

        timings["total_ms"] = round(sum(timings.values()), 2)
        counts = {
            "dense": len(dense_hits),
            "sparse": len(sparse_hits),
            "fused": len(fused),
            "returned": len(results),
        }
        logger.info(
            "retrieval_complete",
            query=plan.query[:120],
            top_k=k,
            reranked=bool(plan.wants_rerank and self.reranker is not None),
            **timings,
        )
        return RetrievalResult(chunks=results, timings=timings, counts=counts)

    async def aretrieve(self, query: str, **kwargs: Any) -> list[RetrievedChunk]:
        """Async entry point; dense and sparse run concurrently in threads.

        Chroma and rank_bm25 are blocking, so each leg goes to a worker thread while
        the async wrapper enforces the real deadline.
        """
        from blackbox.retrieval.timeout import with_timeout

        result = await with_timeout(
            self.aretrieve_detailed(query, **kwargs),
            self.config.retrieval_timeout_ms,
        )
        return result.chunks

    # ---- helpers ----

    def _apply_score_floor(
        self, chunks: list[RetrievedChunk], *, reranked: bool
    ) -> list[RetrievedChunk]:
        """Drop results below `min_relevance_score`, when it is configured.

        The floor is applied to a rerank score only if the result was in fact reranked;
        comparing a rerank score to a floor and a fused score to the same floor would mean
        two different things, so the appropriate score is selected per chunk.

        Leaving the floor unset (the default) keeps every result, which is what the graph's
        grading node needs in order to see weak-but-present evidence and judge it.
        """
        floor = self.config.min_relevance_score
        if floor is None:
            return chunks

        kept: list[RetrievedChunk] = []
        for chunk in chunks:
            if reranked and chunk.rerank_score is not None:
                score = chunk.rerank_score
            else:
                score = chunk.fused_score if chunk.fused_score is not None else 0.0
            if score >= floor:
                kept.append(chunk)
        if len(kept) != len(chunks):
            logger.info(
                "retrieval_score_floor",
                floor=floor,
                dropped=len(chunks) - len(kept),
                kept=len(kept),
            )
        return kept

    def _coerce_filter(
        self, filter: MetadataFilter | dict[str, Any] | None
    ) -> MetadataFilter | None:
        if filter is None:
            return self.default_filter
        if isinstance(filter, MetadataFilter):
            return filter
        parsed = parse_filter(filter)
        return parsed if not parsed.is_empty() else self.default_filter

    def _cache_key(
        self,
        *,
        query: str,
        top_k: int,
        md_filter: MetadataFilter | None,
        use_rerank: bool,
        dense_only: bool,
        sparse_only: bool,
    ) -> str | None:
        if self.cache is None or self.cache_ttl_s <= 0:
            return None
        payload = json.dumps(
            {
                "query": query,
                "top_k": top_k,
                "filter": repr(md_filter),
                "rerank": use_rerank,
                "dense_only": dense_only,
                "sparse_only": sparse_only,
                "candidate_pool": self.config.candidate_pool,
                "fusion": self.config.fusion_method,
                "fusion_k": self.config.fusion_k,
                "dense_weight": self.config.dense_weight,
                "sparse_weight": self.config.sparse_weight,
                "rerank_candidates": self.config.rerank_candidates,
                "score_floor": self.config.min_relevance_score,
            },
            sort_keys=True,
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return f"ret:{digest}"

    # ---- LangChain interface ----

    def _get_relevant_documents(
        self,
        query: str,
        *,
        run_manager: CallbackManagerForRetrieverRun,
    ) -> list[Document]:
        chunks = self.retrieve(query)
        run_manager.on_retriever_end(
            [
                Document(page_content=c.text, metadata={"chunk_id": c.chunk_id})
                for c in chunks
            ]
        )
        return [c.to_document() for c in chunks]

    async def _aget_relevant_documents(
        self,
        query: str,
        *,
        run_manager: AsyncCallbackManagerForRetrieverRun,
    ) -> list[Document]:
        chunks = await self.aretrieve(query)
        return [c.to_document() for c in chunks]


def _rank_single(hits: list[RetrievedChunk], mode: str, top_k: int) -> list[RetrievedChunk]:
    """Give single-mode results a fused score so downstream stages stay uniform."""
    for rank, chunk in enumerate(hits, start=1):
        chunk.fused_rank = rank
        chunk.fused_score = chunk.dense_score if mode == "dense" else chunk.sparse_score
    return hits[:top_k]


def build_hybrid_retriever(
    vectorstore: ChromaVectorStore,
    retrieval_config: RetrievalConfig,
    *,
    default_filter: MetadataFilter | None = None,
    reranker: Any = None,
    top_k: int | None = None,
) -> HybridRetriever:
    return HybridRetriever(
        vectorstore=vectorstore,
        config=retrieval_config,
        default_filter=default_filter,
        reranker=reranker,
        top_k=top_k,
    )
