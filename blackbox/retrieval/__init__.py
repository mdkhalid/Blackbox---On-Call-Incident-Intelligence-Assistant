"""Retrieval layer: hybrid dense + sparse search with fusion, rerank and filtering."""
from __future__ import annotations

from blackbox.config.settings import Settings, get_config, get_settings
from blackbox.ingestion.embeddings import build_embedding_provider
from blackbox.ingestion.vectorstore import ChromaVectorStore
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
from blackbox.retrieval.timeout import Deadline, RetrievalTimeoutError, with_timeout

__all__ = [
    "Deadline",
    "DenseRetriever",
    "FusionConfig",
    "HybridRetriever",
    "MetadataFilter",
    "RetrievalTimeoutError",
    "RetrievedChunk",
    "SparseRetriever",
    "build_default_retriever",
    "build_hybrid_retriever",
    "build_reranker",
    "fuse",
    "last_n_days",
    "parse_filter",
    "to_documents",
    "tokenize",
    "with_timeout",
]


def build_default_retriever(
    settings: Settings | None = None,
    *,
    persist_dir: str | None = None,
    collection_name: str | None = None,
) -> HybridRetriever:
    """Construct the retriever from env settings plus YAML tunables.

    Shared by the API, the eval harness and scripts so all three exercise exactly the same
    pipeline and producing comparable numbers is not an accident.
    """
    settings = settings or get_settings()
    cfg = get_config()
    rc = cfg.retrieval

    embedding_provider = build_embedding_provider(
        rc.embedding_provider,
        model=settings.embedding_model,
        api_key=settings.openai_api_key,
    )
    vectorstore = ChromaVectorStore(
        persist_dir=persist_dir or settings.chroma_persist_dir,
        collection_name=collection_name or rc.collection_name,
        embedding_provider=embedding_provider,
    )
    return build_hybrid_retriever(vectorstore, rc)
