import pytest

from blackbox.retrieval.dense import DenseRetriever
from blackbox.retrieval.filters import MetadataFilter


def test_dense_returns_ranked_chunks_with_similarity_scores(vectorstore):
    r = DenseRetriever(vectorstore)
    hits = r.search("connection pool exhausted checkout-service", top_k=3)
    assert hits
    assert [h.dense_rank for h in hits] == list(range(1, len(hits) + 1))
    scores = [h.dense_score for h in hits]
    assert scores == sorted(scores, reverse=True)
    # Chroma cosine distance is converted to similarity, so this must be in range.
    assert all(-1.0 <= s <= 1.0 for s in scores)
    assert hits[0].metadata["doc_id"]


def test_dense_pushes_filter_into_the_store(vectorstore):
    r = DenseRetriever(vectorstore)
    unfiltered = r.search("connection pool", top_k=10)
    filtered = r.search(
        "connection pool", top_k=10, filter=MetadataFilter(doc_types=["runbook"])
    )
    assert {h.metadata["doc_type"] for h in filtered} == {"runbook"}
    assert len(filtered) < len(unfiltered)


def test_dense_filter_excludes_non_matching_chunks_entirely(vectorstore):
    r = DenseRetriever(vectorstore)
    hits = r.search(
        "anything at all", top_k=10, filter=MetadataFilter(services=["auth-service"])
    )
    assert [h.chunk_id for h in hits] == ["pm-002::Root Cause::0"]


def test_dense_date_filter(vectorstore):
    r = DenseRetriever(vectorstore)
    hits = r.search(
        "incident", top_k=10, filter=MetadataFilter(date_from="2024-01-01")
    )
    assert hits
    assert all("auth-service" not in (h.metadata.get("services") or []) for h in hits)


def test_dense_empty_filter_returns_nothing(vectorstore):
    r = DenseRetriever(vectorstore)
    hits = r.search(
        "checkout", top_k=10, filter=MetadataFilter(services=["does-not-exist"])
    )
    assert hits == []


@pytest.mark.parametrize("top_k", [1, 2, 5])
def test_dense_respects_top_k(vectorstore, top_k):
    r = DenseRetriever(vectorstore)
    assert len(r.search("service", top_k=top_k)) == top_k
