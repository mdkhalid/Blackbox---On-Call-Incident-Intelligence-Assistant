"""Contract tests for the Qdrant dense adapter.

Skipped automatically when qdrant-client or a cluster is unavailable.
"""
from __future__ import annotations

import pytest

try:
    import qdrant_client  # noqa: F401

    _HAS_QDRANT = True
except ImportError:
    _HAS_QDRANT = False


pytestmark = pytest.mark.skipif(not _HAS_QDRANT, reason="qdrant-client not installed")


def test_imports():
    from blackbox.ingestion.qdrant_vectorstore import QdrantVectorStore

    assert QdrantVectorStore is not None


def test_in_memory_upsert_and_query(embedder):
    from blackbox.ingestion.models import Chunk
    from blackbox.ingestion.qdrant_vectorstore import QdrantVectorStore

    vs = QdrantVectorStore(url=":memory:", collection_name="t", embedding_provider=embedder)
    vs.upsert_chunks(
        [
            Chunk(
                chunk_id="c1",
                text="checkout pool exhausted",
                metadata={"doc_id": "d1", "doc_type": "postmortem", "tenant_id": "default"},
            )
        ]
    )
    assert vs.count() == 1
    emb = embedder.embed(["checkout pool"])[0]
    res = vs.query_embedding(emb, top_k=5)
    assert res["ids"][0] == ["c1"]