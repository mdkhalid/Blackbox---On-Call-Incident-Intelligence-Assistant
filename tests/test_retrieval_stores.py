"""The retrieval store contracts.

The sparse leg sits behind a protocol so the in-process BM25 index can be replaced by
OpenSearch without touching the fusion, the rerank or the graph. These tests pin the
contract both adapters must satisfy, and the properties that make the in-process
adapter safe to use until the migration happens.
"""
import pytest

from blackbox.retrieval.filters import MetadataFilter
from blackbox.retrieval.opensearch import OpenSearchSparseStore
from blackbox.retrieval.sparse import SparseRetriever
from blackbox.retrieval.stores import DenseStore, SparseStore, opensearch_clauses


def test_the_in_process_bm25_adapter_satisfies_the_sparse_store_protocol(vectorstore):
    store = SparseRetriever(vectorstore)
    assert isinstance(store, SparseStore)


def test_the_opensearch_adapter_satisfies_the_sparse_store_protocol():
    store = OpenSearchSparseStore(hosts="http://opensearch:9200")
    assert isinstance(store, SparseStore)


def test_the_dense_retriever_satisfies_the_dense_store_protocol(vectorstore):
    from blackbox.retrieval.dense import DenseRetriever

    assert isinstance(DenseRetriever(vectorstore), DenseStore)


def test_the_sparse_store_contract(vectorstore):
    """The behaviour the hybrid retriever relies on, whatever the adapter."""
    store = SparseRetriever(vectorstore)
    store.warm_up()

    hits = store.search("ERR_POOL_EXHAUSTED", top_k=3)
    assert hits
    assert all(hit.sparse_score is not None for hit in hits)
    assert all(hit.sparse_rank is not None for hit in hits)

    found = store.get_chunk(hits[0].chunk_id)
    assert found is not None
    assert found.text == hits[0].text
    assert store.get_chunk("does-not-exist") is None
    assert store.size == len(store._current().ids)


def test_the_sparse_store_contract_filters_before_ranking(vectorstore):
    store = SparseRetriever(vectorstore)
    store.warm_up()

    hits = store.search(
        "connection pool", top_k=10, filter=MetadataFilter(doc_types=["runbook"])
    )
    assert hits
    assert {hit.metadata["doc_type"] for hit in hits} == {"runbook"}


def test_a_chunk_lookup_is_a_dict_lookup_not_a_linear_scan(vectorstore):
    """A linear scan over the corpus is a trap for the next caller at scale."""
    store = SparseRetriever(vectorstore)
    store.warm_up()
    corpus = store._current()

    assert corpus.by_id
    assert corpus.by_id[corpus.ids[0]] == 0
    assert corpus.by_id[corpus.ids[-1]] == len(corpus.ids) - 1


def test_the_index_is_rebuilt_and_published_off_the_request_path(vectorstore, retrieval_config):
    """`rebuild_indexes` publishes a new index; the query after it does not rebuild."""
    from blackbox.ingestion.models import Chunk
    from blackbox.retrieval.hybrid_retriever import build_hybrid_retriever

    retriever = build_hybrid_retriever(vectorstore, retrieval_config)
    retriever.invalidate()
    retriever.rebuild_indexes()

    retriever.vectorstore.upsert_chunks(
        [
            Chunk(
                chunk_id="pm-778::Root Cause::0",
                text="Root cause was ERR_TIMEOUT in search-service.",
                metadata={"doc_id": "pm-778", "doc_type": "postmortem", "section": "Root Cause"},
            )
        ]
    )
    retriever.rebuild_indexes()

    hits = retriever.retrieve("ERR_TIMEOUT", top_k=3, sparse_only=True)
    assert [hit.chunk_id for hit in hits] == ["pm-778::Root Cause::0"]


def test_an_invalidated_index_is_rebuilt_on_demand_as_a_fallback(vectorstore):
    """The development path has no ingestion worker, so the first query rebuilds."""
    from blackbox.ingestion.models import Chunk

    store = SparseRetriever(vectorstore)
    store.warm_up()

    store.vectorstore.upsert_chunks(
        [
            Chunk(
                chunk_id="pm-779::Root Cause::0",
                text="Root cause was ERR_TIMEOUT in search-service.",
                metadata={"doc_id": "pm-779", "doc_type": "postmortem", "section": "Root Cause"},
            )
        ]
    )
    store.invalidate()

    hits = store.search("ERR_TIMEOUT", top_k=3)
    assert [hit.chunk_id for hit in hits] == ["pm-779::Root Cause::0"]


def test_the_filter_is_compiled_into_opensearch_clauses():
    clauses = opensearch_clauses(
        MetadataFilter(
            doc_types=["postmortem"],
            services=["checkout-service"],
            severities=["SEV1"],
            date_from="2024-01-01",
            date_to="2024-12-31",
        )
    )
    assert {"terms": {"doc_type": ["postmortem"]}} in clauses
    assert {"terms": {"services": ["checkout-service"]}} in clauses
    assert {"terms": {"severity": ["SEV1"]}} in clauses
    assert any("range" in clause for clause in clauses)


def test_an_unfiltered_query_compiles_to_no_clauses():
    assert opensearch_clauses(None) == []
    assert opensearch_clauses(MetadataFilter()) == []


def test_the_opensearch_query_carries_the_filter():
    store = OpenSearchSparseStore(hosts="http://opensearch:9200")
    body = store._body(
        "checkout pool", 5, MetadataFilter(doc_types=["postmortem"])
    )
    assert body["size"] == 5
    filters = body["query"]["bool"]["filter"]
    assert {"terms": {"doc_type": ["postmortem"]}} in filters


def test_the_opensearch_query_without_a_filter_has_no_filter_clauses():
    store = OpenSearchSparseStore(hosts="http://opensearch:9200")
    body = store._body("checkout pool", 5, None)
    assert "filter" not in body["query"]["bool"]


@pytest.mark.parametrize("backend", ["bm25", "opensearch"])
def test_the_sparse_backend_is_configurable(backend):
    from blackbox.config.settings import RetrievalConfig

    config = RetrievalConfig(sparse_backend=backend)
    assert config.sparse_backend == backend


def test_an_unknown_sparse_backend_is_rejected():
    from pydantic import ValidationError

    from blackbox.config.settings import RetrievalConfig

    with pytest.raises(ValidationError):
        RetrievalConfig(sparse_backend="elasticsearch")
