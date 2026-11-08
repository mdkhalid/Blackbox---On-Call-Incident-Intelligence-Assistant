import pytest

from blackbox.retrieval.filters import MetadataFilter
from blackbox.retrieval.sparse import SparseRetriever, tokenize


def test_tokenize_keeps_identifiers_whole():
    tokens = tokenize("ERR_POOL_EXHAUSTED")
    # Whole identifier only: splitting would make ERR_AUTH_FAILED match ERR_AUTH_TIMEOUT.
    assert tokens == ["err_pool_exhausted"]


def test_tokenize_splits_hyphenated_service_names():
    tokens = tokenize("checkout-service p99")
    assert "checkout" in tokens
    assert "service" in tokens
    assert "p99" in tokens


def test_tokenize_handles_camel_case():
    tokens = tokenize("CheckoutAPI")
    assert "checkoutapi" in tokens
    assert "checkout" in tokens
    assert "api" in tokens


def test_distinct_error_codes_do_not_collide(vectorstore):
    # The regression this guards: ERR_AUTH_FAILED must not retrieve ERR_POOL_EXHAUSTED.
    r = SparseRetriever(vectorstore)
    hits = r.search("ERR_AUTH_FAILED", top_k=5)
    assert [h.chunk_id for h in hits] == ["pm-002::Root Cause::0"]


def test_bm25_finds_exact_error_code(vectorstore):
    r = SparseRetriever(vectorstore)
    hits = r.search("ERR_POOL_EXHAUSTED", top_k=5)
    assert hits
    assert hits[0].sparse_rank == 1
    assert "ERR_POOL_EXHAUSTED" in hits[0].text


def test_bm25_prioritises_rare_identifier_over_common_words(vectorstore):
    r = SparseRetriever(vectorstore)
    hits = r.search("ERR_AUTH_FAILED", top_k=5)
    assert hits
    assert hits[0].metadata["doc_id"] == "pm-002"


def test_bm25_scores_are_descending(vectorstore):
    r = SparseRetriever(vectorstore)
    hits = r.search("checkout connection pool", top_k=5)
    scores = [h.sparse_score for h in hits]
    assert scores == sorted(scores, reverse=True)
    assert all(s > 0 for s in scores)


def test_bm25_applies_filter_before_ranking(vectorstore):
    r = SparseRetriever(vectorstore)
    hits = r.search(
        "checkout pool", top_k=10, filter=MetadataFilter(doc_types=["runbook"])
    )
    assert hits
    assert {h.metadata["doc_type"] for h in hits} == {"runbook"}


def test_bm25_filter_can_exclude_all(vectorstore):
    r = SparseRetriever(vectorstore)
    hits = r.search(
        "checkout pool", top_k=10, filter=MetadataFilter(doc_types=["does-not-exist"])
    )
    assert hits == []


def test_bm25_returns_nothing_for_unmatched_terms(vectorstore):
    r = SparseRetriever(vectorstore)
    assert r.search("zzzznonexistenttoken", top_k=5) == []


def test_bm25_empty_query_returns_nothing(vectorstore):
    r = SparseRetriever(vectorstore)
    assert r.search("", top_k=5) == []


def test_index_covers_whole_collection(vectorstore, mini_corpus):
    r = SparseRetriever(vectorstore)
    assert r.size == len(mini_corpus)


def test_get_chunk_by_id(vectorstore):
    r = SparseRetriever(vectorstore)
    chunk = r.get_chunk("pm-001::Root Cause::0")
    assert chunk is not None
    assert "ERR_POOL_EXHAUSTED" in chunk.text
    assert r.get_chunk("missing") is None


def test_invalidate_rebuilds_index_after_new_chunks(vectorstore, embedder):
    from blackbox.ingestion.models import Chunk

    r = SparseRetriever(vectorstore)
    before = r.search("ERR_TIMEOUT", top_k=5)
    assert before == []

    vectorstore.upsert_chunks(
        [
            Chunk(
                chunk_id="pm-999::Root Cause::0",
                text="Root cause was ERR_TIMEOUT in search-service.",
                metadata={"doc_id": "pm-999", "doc_type": "postmortem", "section": "Root Cause"},
            )
        ]
    )
    # Stale cache still misses it; invalidate picks it up.
    assert r.search("ERR_TIMEOUT", top_k=5) == []
    r.invalidate()
    after = r.search("ERR_TIMEOUT", top_k=5)
    assert [h.chunk_id for h in after] == ["pm-999::Root Cause::0"]


@pytest.mark.parametrize("query", ["p99", "p99 latency", "P99 LATENCY SPIKING"])
def test_bm25_is_case_insensitive(vectorstore, query):
    r = SparseRetriever(vectorstore)
    assert r.search(query, top_k=5)
