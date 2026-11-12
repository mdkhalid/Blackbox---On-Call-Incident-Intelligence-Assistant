"""Retrieval layer: hybrid dense + sparse search with fusion, rerank and filtering."""
from __future__ import annotations

from typing import Any

from blackbox.config.settings import RetrievalConfig, Settings, get_config, get_settings
from blackbox.ingestion.embeddings import build_embedding_provider
from blackbox.retrieval.dense import DenseRetriever
from blackbox.retrieval.filters import MetadataFilter, last_n_days, parse_filter
from blackbox.retrieval.fusion import FusionConfig, fuse
from blackbox.retrieval.hybrid_retriever import (
    HybridRetriever,
    build_hybrid_retriever,
)
from blackbox.retrieval.models import RetrievedChunk, to_documents
from blackbox.retrieval.rerank import build_reranker
from blackbox.retrieval.sparse import SparseRetriever, tokenize
from blackbox.retrieval.stores import DenseStore, SparseStore
from blackbox.retrieval.timeout import Deadline, RetrievalTimeoutError, with_timeout

__all__ = [
    "Deadline",
    "DenseRetriever",
    "DenseStore",
    "FusionConfig",
    "HybridRetriever",
    "MetadataFilter",
    "RetrievalTimeoutError",
    "RetrievedChunk",
    "SparseRetriever",
    "SparseStore",
    "build_default_retriever",
    "build_hybrid_retriever",
    "build_reranker",
    "build_sparse_store",
    "fuse",
    "last_n_days",
    "parse_filter",
    "to_documents",
    "tokenize",
    "with_timeout",
]


def build_vectorstore(
    settings: Settings | None = None,
    retrieval_config: RetrievalConfig | None = None,
    *,
    persist_dir: str | None = None,
    collection_name: str | None = None,
    embedding_provider: Any = None,
):
    """The dense leg's adapter, chosen by configuration.

    `chroma` keeps the collection on disk (development default); `qdrant` keeps it
    in the cluster.
    """
    settings = settings or get_settings()
    rc = retrieval_config if retrieval_config is not None else get_config().retrieval
    backend = getattr(rc, "dense_backend", "chroma")
    name = collection_name or rc.collection_name
    if backend == "qdrant":
        from blackbox.ingestion.qdrant_vectorstore import QdrantVectorStore

        return QdrantVectorStore(
            url=settings.qdrant_url,
            collection_name=name,
            embedding_provider=embedding_provider,
            api_key=settings.qdrant_api_key,
        )
    from blackbox.ingestion.vectorstore import ChromaVectorStore

    return ChromaVectorStore(
        persist_dir=persist_dir or settings.chroma_persist_dir,
        collection_name=name,
        embedding_provider=embedding_provider,
    )


def build_sparse_store(
    settings: Settings | None = None,
    retrieval_config: RetrievalConfig | None = None,
):
    """The sparse leg's adapter, chosen by configuration.

    `bm25` keeps the index in the pod's memory, which is the development default.
    `opensearch` keeps it in the cluster, which is what the target scale needs.
    """
    settings = settings or get_settings()
    rc = retrieval_config if retrieval_config is not None else get_config().retrieval
    if rc.sparse_backend == "opensearch":
        from blackbox.retrieval.opensearch import OpenSearchSparseStore

        return OpenSearchSparseStore(
            hosts=settings.opensearch_hosts,
            index=settings.opensearch_index,
            username=settings.opensearch_username,
            password=settings.opensearch_password,
            verify_certs=settings.opensearch_verify_certs,
        )
    return None


def build_default_retriever(
    settings: Settings | None = None,
    *,
    persist_dir: str | None = None,
    collection_name: str | None = None,
    retrieval_config: RetrievalConfig | None = None,
    tenant_id: str | None = None,
) -> HybridRetriever:
    """Construct the retriever from env settings plus YAML tunables.

    Shared by the API, the eval harness and scripts so all three exercise exactly the same
    pipeline and producing comparable numbers is not an accident.
    """
    settings = settings or get_settings()
    rc = retrieval_config if retrieval_config is not None else get_config().retrieval

    embedding_provider = build_embedding_provider(
        rc.embedding_provider,
        model=settings.embedding_model,
        api_key=settings.openai_api_key,
    )
    vectorstore = build_vectorstore(
        settings=settings,
        retrieval_config=rc,
        persist_dir=persist_dir,
        collection_name=collection_name,
        embedding_provider=embedding_provider,
    )
    return build_hybrid_retriever(
        vectorstore, rc, sparse_store=build_sparse_store(settings, rc),
        tenant_id=tenant_id,
    )
