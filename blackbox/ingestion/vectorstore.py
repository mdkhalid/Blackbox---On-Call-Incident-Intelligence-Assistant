"""Chroma vector store wrapper with idempotent upsert."""
from __future__ import annotations

from collections.abc import Iterable

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
            metas.append(c.metadata)
        if not ids:
            return 0
        embeddings = self.embedding_provider.embed(docs)
        self.collection.upsert(ids=ids, documents=docs, metadatas=metas, embeddings=embeddings)
        return len(ids)

    def delete_by_doc(self, doc_id: str) -> None:
        try:
            self.collection.delete(where={"doc_id": doc_id})
        except Exception:  # noqa: BLE001, S110
            pass

    def count(self) -> int:
        return self.collection.count()

    def query(self, text: str, top_k: int = 5, where: dict | None = None):
        emb = self.embedding_provider.embed([text])[0]
        res = self.collection.query(query_embeddings=[emb], n_results=top_k, where=where)
        return res
