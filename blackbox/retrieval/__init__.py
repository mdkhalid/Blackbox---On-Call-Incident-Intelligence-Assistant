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


def _region_scoped(name: str, settings: Settings) -> str:
    """Append the region id when per-region indexes are configured.

    Two regions cannot share one physical index name and stay a failover pair: a
    rebuild in the secondary would otherwise overwrite the primary. The suffix makes
    each region's index distinct and independently rebuildable.
    """
    try:
        region_cfg = get_config().region
    except Exception:  # noqa: BLE001 - config is optional for this helper
        return name
    if getattr(region_cfg, "region_suffix", False):
        return f"{name}_{settings.region}"
    return name


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
    name = _region_scoped(collection_name or rc.collection_name, settings)
    if backend == "qdrant":
        from blackbox.ingestion.qdrant_vectorstore import QdrantVectorStore

        return QdrantVectorStore(
            url=settings.qdrant_url,
            collection_name=name,
            embedding_provider=embedding_provider,
            api_key=settings.qdrant_api_key,
            replication_factor=settings.qdrant_replication_factor,
            shard_number=settings.qdrant_shard_number,
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

        base = settings.opensearch_alias or settings.opensearch_index
        return OpenSearchSparseStore(
            hosts=settings.opensearch_hosts,
            index=_region_scoped(settings.opensearch_index, settings),
            username=settings.opensearch_username,
            password=settings.opensearch_password,
            verify_certs=settings.opensearch_verify_certs,
            alias=_region_scoped(base, settings) if settings.opensearch_alias else None,
            shard_count=settings.opensearch_shard_count,
            replica_count=settings.opensearch_replica_count,
        )
    return None


def build_default_retriever(
    settings: Settings | None = None,
    *,
    persist_dir: str | None = None,
    collection_name: str | None = None,
    retrieval_config: RetrievalConfig | None = None,
    tenant_id: str | None = None,
) -> Any:
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
    retriever = build_hybrid_retriever(
        vectorstore, rc, sparse_store=build_sparse_store(settings, rc),
        tenant_id=tenant_id,
    )
    shadow_backend = getattr(rc, "shadow_dense_backend", None)
    if shadow_backend:
        shadow_rc = rc.model_copy(update={"dense_backend": shadow_backend})
        shadow_vs = build_vectorstore(
            settings=settings,
            retrieval_config=shadow_rc,
            persist_dir=persist_dir,
            collection_name=collection_name,
            embedding_provider=embedding_provider,
        )
        shadow_retriever = build_hybrid_retriever(
            shadow_vs, shadow_rc, sparse_store=build_sparse_store(settings, shadow_rc),
            tenant_id=tenant_id,
        )
        from blackbox.retrieval.shadow import ShadowingRetriever

        return ShadowingRetriever(retriever, shadow_retriever)
    return retriever
