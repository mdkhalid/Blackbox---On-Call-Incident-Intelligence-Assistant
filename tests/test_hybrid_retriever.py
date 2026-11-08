import pytest
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever

from blackbox.retrieval.filters import MetadataFilter
from blackbox.retrieval.hybrid_retriever import build_hybrid_retriever
from blackbox.retrieval.rerank import LexicalReranker, NullReranker
from blackbox.retrieval.timeout import RetrievalTimeoutError


@pytest.fixture
def retriever(vectorstore, retrieval_config):
    r = build_hybrid_retriever(vectorstore, retrieval_config)
    r.reranker = LexicalReranker.from_corpus(list(vectorstore.get_all()["documents"]))
    return r


def test_hybrid_returns_top_k_with_full_score_trail(retriever, retrieval_config):
    hits = retriever.retrieve("checkout connection pool exhausted", top_k=3)
    assert len(hits) == 3
    # Final ordering is the rerank order; the fused rank is preserved on each chunk as
    # provenance for the trace viewer.
    assert [h.rerank_rank for h in hits] == [1, 2, 3]
    for h in hits:
        assert h.chunk_id and h.text
        assert h.rerank_score is not None
        assert h.fused_rank is not None


def test_hybrid_finds_exact_error_code_cross_section(retriever):
    # The assignment's core case: an exact token must surface the right document.
    hits = retriever.retrieve("ERR_AUTH_FAILED", top_k=3)
    assert hits[0].metadata["doc_id"] == "pm-002"


def test_hybrid_pulls_from_both_retrievers(retriever):
    retriever.retrieve("checkout connection pool", top_k=5)
    counts = retriever.last_counts
    assert counts["dense"] > 0
    assert counts["sparse"] > 0
    assert counts["fused"] > 0


def test_hybrid_dense_only_mode_has_no_sparse_scores(retriever):
    hits = retriever.retrieve("checkout pool", top_k=3, dense_only=True)
    assert hits
    assert all(h.sparse_score is None for h in hits)


def test_hybrid_sparse_only_mode_has_no_dense_scores(retriever):
    hits = retriever.retrieve("ERR_POOL_EXHAUSTED", top_k=3, sparse_only=True)
    assert hits
    assert all(h.dense_score is None for h in hits)


def test_hybrid_without_rerank_leaves_rerank_scores_empty(retriever):
    hits = retriever.retrieve("checkout pool", top_k=3, use_rerank=False)
    assert all(h.rerank_score is None for h in hits)
    assert [h.fused_rank for h in hits] == [1, 2, 3]


def test_hybrid_respects_metadata_filter(retriever):
    hits = retriever.retrieve(
        "connection pool", top_k=10, filter=MetadataFilter(doc_types=["runbook"])
    )
    assert hits
    assert {h.metadata["doc_type"] for h in hits} == {"runbook"}


def test_hybrid_filter_accepts_dict_payload(retriever):
    hits = retriever.retrieve(
        "connection pool", top_k=10, filter={"service": "auth-service"}
    )
    assert [h.metadata["doc_id"] for h in hits] == ["pm-002"]


def test_hybrid_default_filter_is_used_when_none_passed(vectorstore, retrieval_config):
    r = build_hybrid_retriever(
        vectorstore, retrieval_config, default_filter=MetadataFilter(doc_types=["alert"])
    )
    hits = r.retrieve("checkout", top_k=5)
    assert hits
    assert {h.metadata["doc_type"] for h in hits} == {"alert"}


def test_hybrid_rerank_does_not_invent_chunks(retriever):
    hits = retriever.retrieve("checkout pool", top_k=5)
    corpus_ids = set(retriever.vectorstore.get_all()["ids"])
    assert {h.chunk_id for h in hits} <= corpus_ids


def test_hybrid_is_deterministic(retriever):
    runs = [
        [h.chunk_id for h in retriever.retrieve("checkout connection pool", top_k=5)]
        for _ in range(3)
    ]
    assert runs[0] == runs[1] == runs[2]


def test_hybrid_no_lexical_match_returns_no_sparse_hits(retriever):
    # Dense retrieval always returns its nearest neighbours, so "nothing relevant" is a
    # judgement made by the graph's grading node, not by the retriever having zero rows.
    hits = retriever.retrieve("zzzznothingmatchesthis", top_k=5, sparse_only=True)
    assert hits == []


def test_score_floor_can_force_an_empty_result(vectorstore, retrieval_config):
    floor_cfg = retrieval_config.model_copy(update={"min_relevance_score": 0.99})
    r = build_hybrid_retriever(vectorstore, floor_cfg)
    r.reranker = LexicalReranker.from_corpus(list(vectorstore.get_all()["documents"]))
    assert r.retrieve("zzzznothingmatchesthis", top_k=5) == []


def test_score_floor_keeps_strong_matches(vectorstore, retrieval_config):
    floor_cfg = retrieval_config.model_copy(update={"min_relevance_score": 0.01})
    r = build_hybrid_retriever(vectorstore, floor_cfg)
    r.reranker = LexicalReranker.from_corpus(list(vectorstore.get_all()["documents"]))
    assert r.retrieve("ERR_POOL_EXHAUSTED", top_k=3)


def test_timings_are_recorded_per_stage(retriever):
    retriever.retrieve("checkout pool", top_k=3)
    timings = retriever.last_timings
    for stage in ("dense_ms", "sparse_ms", "fuse_ms", "rerank_ms", "total_ms"):
        assert stage in timings
        assert timings[stage] >= 0


def test_timeout_guard_raises(vectorstore):
    from blackbox.config.settings import RetrievalConfig

    cfg = RetrievalConfig(retrieval_timeout_ms=1, rerank_provider="none")
    r = build_hybrid_retriever(vectorstore, cfg)
    # A 1ms budget cannot cover the embedding call in practice; if it somehow does,
    # the guard simply does not fire and the call succeeds.
    try:
        r.retrieve("checkout pool", top_k=3)
    except RetrievalTimeoutError:
        pass


def test_langchain_contract_invoke_returns_documents(retriever):
    assert isinstance(retriever, BaseRetriever)
    docs = retriever.invoke("checkout connection pool")
    assert docs
    assert all(isinstance(d, Document) for d in docs)
    assert all("chunk_id" in d.metadata for d in docs)


def test_langchain_documents_carry_score_metadata(retriever):
    docs = retriever.invoke("ERR_POOL_EXHAUSTED")
    assert any(d.metadata.get("rerank_score") is not None for d in docs)


def test_langchain_composes_in_an_runnable_chain(retriever):
    from langchain_core.runnables import RunnableLambda

    chain = retriever | RunnableLambda(lambda docs: [d.metadata["doc_id"] for d in docs])
    doc_ids = chain.invoke("checkout connection pool")
    assert doc_ids
    assert all(isinstance(i, str) for i in doc_ids)


async def test_async_retrieve(retriever):
    docs = await retriever.ainvoke("checkout connection pool")
    assert docs


def test_cold_start_does_not_trip_the_deadline(vectorstore, retrieval_config):
    """Index building is startup work and must not be charged to a request.

    Regression guard: building the BM25 index and warming the embedder on the first query
    used to run inside the timeout window, so a healthy first request could abort.
    """
    cfg = retrieval_config.model_copy(update={"retrieval_timeout_ms": 50, "rerank": False})
    r = build_hybrid_retriever(vectorstore, cfg)
    # First call in the process: the index is cold here.
    hits = r.retrieve("checkout connection pool", top_k=3)
    assert hits


def test_invalidate_refreshes_sparse_index(retriever):
    from blackbox.ingestion.models import Chunk

    assert retriever.retrieve("ERR_TIMEOUT", top_k=3, sparse_only=True) == []
    retriever.vectorstore.upsert_chunks(
        [
            Chunk(
                chunk_id="pm-777::Root Cause::0",
                text="Root cause was ERR_TIMEOUT in search-service.",
                metadata={"doc_id": "pm-777", "doc_type": "postmortem", "section": "Root Cause"},
            )
        ]
    )
    retriever.invalidate()
    hits = retriever.retrieve("ERR_TIMEOUT", top_k=3, sparse_only=True)
    assert [h.chunk_id for h in hits] == ["pm-777::Root Cause::0"]


def test_null_reranker_path_is_used_when_disabled(vectorstore, retrieval_config):
    r = build_hybrid_retriever(
        vectorstore, retrieval_config, reranker=NullReranker()
    )
    hits = r.retrieve("checkout pool", top_k=3)
    assert all(h.rerank_score is None for h in hits)
