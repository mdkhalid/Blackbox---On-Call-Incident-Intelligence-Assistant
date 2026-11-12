"""Chroma vector store wrapper with idempotent upsert."""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any, cast

import chromadb


class ChromaVectorStore:
    def __init__(self, persist_dir: str, collection_name: str, embedding_provider):
        self.client = chromadb.PersistentClient(path=persist_dir)
        self.embedding_provider = embedding_provider
        self.collection = self.client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"},
        )

    def upsert_chunks(self, chunks: Iterable) -> int:
        ids = []
        docs = []
        metas = []
        for c in chunks:
            ids.append(c.chunk_id)
            docs.append(c.text)
            meta: dict[str, Any] = dict(c.metadata or {})
            # Tenant scoping is mandatory: every stored chunk belongs to a tenant.
            # A chunk written without one is treated as the default tenant rather
            # than as "all tenants", so it can never leak across the boundary.
            meta.setdefault("tenant_id", "default")
            metas.append(meta)
        if not ids:
            return 0
        embeddings = self.embedding_provider.embed(docs)
        self.collection.upsert(
            ids=ids, documents=docs, metadatas=cast(Any, metas), embeddings=embeddings
        )
        return len(ids)

    def delete_by_doc(self, doc_id: str) -> None:
        try:
            self.collection.delete(where={"doc_id": doc_id})
        except Exception:  # noqa: BLE001, S110
            pass

    def count(self) -> int:
        return self.collection.count()

    def has_doc(self, doc_id: str) -> bool:
        """Whether any chunk for this document is actually present in the collection."""
        try:
            result = self.collection.get(
                where={"doc_id": doc_id}, limit=1, include=["metadatas"]
            )
        except Exception:  # noqa: BLE001 - an empty/missing collection means "not present"
            return False
        return bool(result.get("ids"))

    def query(self, text: str, top_k: int = 5, where: dict | None = None):
        emb = self.embedding_provider.embed([text])[0]
        res = self.collection.query(query_embeddings=[emb], n_results=top_k, where=where)
        return res

    def query_embedding(
        self, embedding: list[float], top_k: int = 5, where: dict | None = None
    ) -> dict:
        # Chroma's stub types the embedding argument as a numpy-compatible sequence;
        # a plain list of floats is accepted at runtime but does not satisfy the
        # invariant element type, so the argument is passed through as `Any`.
        embeddings: list[Any] = [embedding]
        result = self.collection.query(
            query_embeddings=embeddings, n_results=top_k, where=where
        )
        return cast(dict, result)

    def get_all(self, where: dict | None = None) -> dict:
        """Fetch every matching chunk. Used to build the in-process BM25 index."""
        kwargs: dict = {"include": ["documents", "metadatas"]}
        if where:
            kwargs["where"] = where
        return cast(dict, self.collection.get(**kwargs))
