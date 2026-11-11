import pytest

from blackbox.cache import TTLCache
from blackbox.config.settings import RetrievalConfig
from blackbox.retrieval.hybrid_retriever import build_hybrid_retriever


@pytest.fixture
def cached_retriever(vectorstore):
    config = RetrievalConfig(
        top_k=3, candidate_pool=10, rerank=False, rerank_provider="none"
    )
    retriever = build_hybrid_retriever(vectorstore, config)
    retriever.cache = TTLCache(default_ttl_s=60)
    retriever.cache_ttl_s = 60
    return retriever


def test_second_identical_query_is_served_from_cache(cached_retriever):
    first = cached_retriever.retrieve_detailed("checkout connection pool", top_k=3)
    assert "cache_ms" not in first.timings

    second = cached_retriever.retrieve_detailed("checkout connection pool", top_k=3)
    assert "cache_ms" in second.timings
    assert [chunk.chunk_id for chunk in first.chunks] == [
        chunk.chunk_id for chunk in second.chunks
    ]
    assert second.chunks[0].fused_score == first.chunks[0].fused_score


def test_different_top_k_is_a_different_cache_entry(cached_retriever):
    cached_retriever.retrieve_detailed("checkout connection pool", top_k=3)
    result = cached_retriever.retrieve_detailed("checkout connection pool", top_k=2)
    assert "cache_ms" not in result.timings


def test_filters_are_part_of_the_cache_key(cached_retriever):
    cached_retriever.retrieve_detailed("connection pool", top_k=3)
    result = cached_retriever.retrieve_detailed(
        "connection pool", top_k=3, filter={"doc_types": ["runbook"]}
    )
    assert "cache_ms" not in result.timings


def test_invalidate_clears_retrieval_cache(cached_retriever):
    cached_retriever.retrieve_detailed("checkout connection pool", top_k=3)
    cached_retriever.invalidate()
    result = cached_retriever.retrieve_detailed("checkout connection pool", top_k=3)
    assert "cache_ms" not in result.timings


def test_cache_disabled_when_ttl_is_zero(cached_retriever):
    cached_retriever.cache_ttl_s = 0
    cached_retriever.retrieve_detailed("checkout connection pool", top_k=3)
    result = cached_retriever.retrieve_detailed("checkout connection pool", top_k=3)
    assert "cache_ms" not in result.timings
