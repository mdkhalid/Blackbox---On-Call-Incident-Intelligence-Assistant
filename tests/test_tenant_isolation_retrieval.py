"""Cross-tenant retrieval isolation (Phase 2 exit gate).

A tenant's retrieval must never return another tenant's chunks, even when the
caller passes an empty or forged filter.
"""
from __future__ import annotations

import pytest

from blackbox.ingestion.models import Chunk
from blackbox.ingestion.vectorstore import ChromaVectorStore
from blackbox.retrieval import MetadataFilter, build_hybrid_retriever


def _chunk(chunk_id, tenant_id, text="checkout pool exhausted"):
    return Chunk(
        chunk_id=chunk_id,
        text=text,
        metadata={
            "doc_id": chunk_id.split("::")[0],
            "doc_type": "postmortem",
            "section": "Root Cause",
            "tenant_id": tenant_id,
        },
    )


@pytest.fixture
def store(tmp_path, embedder):
    vs = ChromaVectorStore(str(tmp_path / "chroma"), "test_chunks", embedder)
    vs.upsert_chunks(
        [
            _chunk("a::0", "tenant-a", "tenant A secret runbook"),
            _chunk("b::0", "tenant-b", "tenant B secret runbook"),
        ]
    )
    return vs


def test_empty_filter_does_not_cross_tenants(store):
    retriever = build_hybrid_retriever(store, _cfg(), tenant_id="tenant-a")
    hits = retriever.retrieve("runbook", top_k=5)
    assert all(h.metadata.get("tenant_id") == "tenant-a" for h in hits)
    assert not any(h.metadata.get("tenant_id") == "tenant-b" for h in hits)


def test_forged_filter_cannot_escape_tenant(store):
    retriever = build_hybrid_retriever(store, _cfg(), tenant_id="tenant-a")
    # Forged filter asks for tenant-b's data.
    hits = retriever.retrieve(
        "runbook", top_k=5, filter=MetadataFilter(tenant_id="tenant-b")
    )
    # The retriever's tenant is authoritative: tenant-a sees only its own.
    assert all(h.metadata.get("tenant_id") == "tenant-a" for h in hits)
    assert not any(h.metadata.get("tenant_id") == "tenant-b" for h in hits)


def test_tenant_b_isolated_from_tenant_a(store):
    retriever = build_hybrid_retriever(store, _cfg(), tenant_id="tenant-b")
    hits = retriever.retrieve("runbook", top_k=5)
    assert all(h.metadata.get("tenant_id") == "tenant-b" for h in hits)


def test_empty_tenant_rejected(store):
    retriever = build_hybrid_retriever(store, _cfg(), tenant_id="tenant-a")
    with pytest.raises(ValueError):
        retriever.retrieve("runbook", top_k=5, tenant_id="")


def _cfg():
    from blackbox.config.settings import get_config

    return get_config().retrieval