"""The OpenSearch sparse adapter.

The adapter is written against a real cluster, so these tests run against one when it
is reachable. When it is not, the live tests skip and the query-compilation tests —
the parts that can be checked without a cluster — still run.

The contract test is the important one: the OpenSearch adapter and the in-process BM25
adapter must agree on which chunks match, or swapping one for the other would silently
change retrieval quality.
"""
import os

import pytest

from blackbox.ingestion.models import Chunk
from blackbox.retrieval.filters import MetadataFilter
from blackbox.retrieval.opensearch import FILTER_FIELDS, OpenSearchSparseStore
from blackbox.retrieval.sparse import SparseRetriever, tokenize
from blackbox.retrieval.stores import SparseStore

DEFAULT_HOSTS = "http://127.0.0.1:9200"


@pytest.fixture
def opensearch_store():
    """A store over a clean index.

    The index is dropped and recreated per test, so one test's documents cannot leak
    into the next one's results.
    """
    hosts = os.environ.get("OPENSEARCH_HOSTS", DEFAULT_HOSTS)
    index = os.environ.get("OPENSEARCH_INDEX", "blackbox_test_chunks")
    store = OpenSearchSparseStore(hosts=hosts, index=index)
    try:
        store.client.cluster.health()
    except Exception as exc:  # noqa: BLE001 - no cluster means the live tests skip
        pytest.skip(f"OpenSearch is not reachable at {hosts}: {exc}")
    store.drop_index()
    yield store
    store.drop_index()


@pytest.fixture
def corpus() -> list[Chunk]:
    return [
        Chunk(
            chunk_id="pm-001::Root Cause::0",
            text=(
                "The root cause was ERR_POOL_EXHAUSTED in checkout-service. The database "
                "connection pool became saturated because upstream slowed down."
            ),
            metadata={
                "doc_id": "pm-001",
                "doc_type": "postmortem",
                "section": "Root Cause",
                "services": ["checkout-service", "payment-gateway"],
                "error_codes": ["ERR_POOL_EXHAUSTED"],
                "severity": "SEV1",
                "date_ord": 739014,
            },
        ),
        Chunk(
            chunk_id="pm-002::Root Cause::0",
            text=(
                "Root cause: a bad TLS certificate caused ERR_AUTH_FAILED in auth-service. "
                "Tokens were rejected after certificate rotation."
            ),
            metadata={
                "doc_id": "pm-002",
                "doc_type": "postmortem",
                "section": "Root Cause",
                "services": ["auth-service"],
                "error_codes": ["ERR_AUTH_FAILED"],
                "severity": "SEV2",
                "date_ord": 738826,
            },
        ),
        Chunk(
            chunk_id="rb-001::Symptom::0",
            text=(
                "checkout-service reports ERR_POOL_EXHAUSTED; p99 latency is spiking and "
                "requests pile up in the connection pool."
            ),
            metadata={
                "doc_id": "rb-001",
                "doc_type": "runbook",
                "section": "Symptom",
                "services": ["checkout-service"],
                "error_codes": ["ERR_POOL_EXHAUSTED"],
                "severity": "SEV1",
                "date_ord": 738959,
            },
        ),
    ]


# ---- tests that need no cluster ----


def test_the_adapter_satisfies_the_sparse_store_protocol():
    assert isinstance(OpenSearchSparseStore(hosts=DEFAULT_HOSTS), SparseStore)


def test_the_query_is_tokenized_with_the_shared_tokenizer():
    """Lucene would split `ERR_POOL_EXHAUSTED`; the shared tokenizer must not."""
    store = OpenSearchSparseStore(hosts=DEFAULT_HOSTS)
    body = store._body("ERR_POOL_EXHAUSTED checkout-service", 5, None)
    query_text = body["query"]["bool"]["must"][0]["match"]["tokens"]
    assert "err_pool_exhausted" in query_text
    assert "checkout" in query_text and "service" in query_text
    # The identifier is one term, exactly as the BM25 adapter scores it.
    assert query_text.split() == tokenize("ERR_POOL_EXHAUSTED checkout-service")


def test_an_empty_query_matches_nothing():
    store = OpenSearchSparseStore(hosts=DEFAULT_HOSTS)
    body = store._body("", 5, None)
    assert body["query"]["bool"]["must"] == [{"match_none": {}}]


def test_the_filter_is_compiled_into_the_query():
    store = OpenSearchSparseStore(hosts=DEFAULT_HOSTS)
    body = store._body("checkout", 5, MetadataFilter(doc_types=["postmortem"]))
    assert {"terms": {"doc_type": ["postmortem"]}} in body["query"]["bool"]["filter"]


def test_the_filter_fields_are_declared_as_keywords():
    from blackbox.retrieval.opensearch import INDEX_MAPPING

    properties = INDEX_MAPPING["mappings"]["properties"]
    for field in FILTER_FIELDS:
        assert properties[field]["type"] == "keyword"
    assert properties["tokens"]["analyzer"] == "whitespace"


def test_the_indexed_document_carries_the_filter_fields():
    store = OpenSearchSparseStore(hosts=DEFAULT_HOSTS)
    chunk = Chunk(
        chunk_id="c1",
        text="checkout-service reports ERR_POOL_EXHAUSTED",
        metadata={
            "doc_id": "pm-001",
            "doc_type": "postmortem",
            "services": ["checkout-service"],
            "error_codes": ["ERR_POOL_EXHAUSTED"],
            "date_ord": 739014,
        },
    )
    document = store._action(chunk)["_source"]
    assert document["doc_type"] == "postmortem"
    assert document["services"] == ["checkout-service"]
    assert document["date_ord"] == 739014
    assert document["tokens"] == " ".join(tokenize(chunk.text))


# ---- tests that need a cluster ----


def test_a_chunk_can_be_indexed_and_found(opensearch_store, corpus):
    opensearch_store.upsert_chunks(corpus)
    assert opensearch_store.size == len(corpus)

    hits = opensearch_store.search("ERR_POOL_EXHAUSTED", top_k=3)
    assert hits
    assert {hit.chunk_id for hit in hits} == {
        "pm-001::Root Cause::0",
        "rb-001::Symptom::0",
    }
    assert all(hit.sparse_score is not None for hit in hits)
    assert [hit.sparse_rank for hit in hits] == [1, 2]


def test_an_exact_error_code_does_not_match_a_neighbouring_one(opensearch_store, corpus):
    """The case the whole sparse leg exists for.

    `ERR_AUTH_FAILED` and `ERR_AUTH_TIMEOUT` share the `err` and `auth` fragments, so
    a naive analyzer would match both documents. The identifier must stay whole.
    """
    opensearch_store.upsert_chunks(corpus)
    hits = opensearch_store.search("ERR_AUTH_TIMEOUT", top_k=5)
    assert [hit.chunk_id for hit in hits] == []


def test_the_filter_is_applied_inside_the_cluster(opensearch_store, corpus):
    opensearch_store.upsert_chunks(corpus)
    hits = opensearch_store.search(
        "checkout-service pool", top_k=10, filter=MetadataFilter(doc_types=["runbook"])
    )
    assert hits
    assert {hit.metadata["doc_type"] for hit in hits} == {"runbook"}


def test_a_list_filter_matches_any_requested_value(opensearch_store, corpus):
    opensearch_store.upsert_chunks(corpus)
    hits = opensearch_store.search(
        "ERR_POOL_EXHAUSTED", top_k=10, filter=MetadataFilter(services=["auth-service"])
    )
    assert hits == []


def test_a_chunk_can_be_read_back_by_id(opensearch_store, corpus):
    opensearch_store.upsert_chunks(corpus)
    found = opensearch_store.get_chunk("pm-002::Root Cause::0")
    assert found is not None
    assert "ERR_AUTH_FAILED" in found.text
    assert found.metadata["doc_id"] == "pm-002"
    assert opensearch_store.get_chunk("does-not-exist") is None


def test_deleting_a_document_removes_its_chunks(opensearch_store, corpus):
    opensearch_store.upsert_chunks(corpus)
    opensearch_store.delete_by_doc("pm-002")
    hits = opensearch_store.search("ERR_AUTH_FAILED", top_k=5)
    assert hits == []


def test_reingesting_the_same_chunks_does_not_duplicate_them(opensearch_store, corpus):
    opensearch_store.upsert_chunks(corpus)
    opensearch_store.upsert_chunks(corpus)
    assert opensearch_store.size == len(corpus)


def test_the_two_sparse_adapters_agree_on_what_matches(
    opensearch_store, mini_corpus, vectorstore
):
    """The contract: swapping the adapter must not change what retrieval returns."""
    opensearch_store.upsert_chunks(mini_corpus)
    in_process = SparseRetriever(vectorstore)
    in_process.rebuild()

    queries = [
        "ERR_POOL_EXHAUSTED",
        "checkout-service connection pool",
        "ERR_AUTH_FAILED",
        "p99 latency spiking",
    ]
    for query in queries:
        remote = [hit.chunk_id for hit in opensearch_store.search(query, top_k=5)]
        local = [hit.chunk_id for hit in in_process.search(query, top_k=5)]
        assert remote == local, f"the adapters disagree on {query!r}"


def test_the_ingest_pipeline_writes_the_sparse_index(opensearch_store, tmp_path):
    """Ingestion must fill the search index, not only the dense collection."""
    import sys

    from blackbox.ingestion.embeddings import FakeEmbeddingProvider
    from blackbox.ingestion.entity_extraction.store import SqlAlchemyEntityStore
    from blackbox.ingestion.pipeline import IngestionPipeline
    from blackbox.ingestion.vectorstore import ChromaVectorStore
    from seed.generate_corpus import main as gen_main

    sys.argv = ["x", "--out", str(tmp_path / "corpus")]
    gen_main()
    root = tmp_path / "corpus"

    vectorstore = ChromaVectorStore(
        str(tmp_path / "chroma"), "coll", FakeEmbeddingProvider(dim=4)
    )
    entity_store = SqlAlchemyEntityStore(f"sqlite:///{tmp_path / 'e.db'}")
    entity_store.init()

    pipeline = IngestionPipeline(
        vectorstore, entity_store, sparse_store=opensearch_store, batch_size=32
    )
    results = pipeline.ingest_directory(root)

    assert len(results) == 60
    assert opensearch_store.size == vectorstore.count()
    assert opensearch_store.size > 0

    # The index is searchable and returns the same documents the dense side does.
    hits = opensearch_store.search("ERR_POOL_EXHAUSTED checkout-service", top_k=5)
    assert hits
    assert all(hit.metadata.get("doc_id") for hit in hits)
