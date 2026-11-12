"""Dense retrieval: embedding similarity over the Chroma collection."""
from __future__ import annotations

from blackbox.ingestion.vectorstore import ChromaVectorStore
from blackbox.retrieval.filters import MetadataFilter
from blackbox.retrieval.models import RetrievedChunk


class DenseRetriever:
    """Cosine-similarity retrieval.

    Chroma returns cosine *distance*; we convert to similarity (`1 - distance`) so every
    score in the pipeline is "higher is better".
    """

    def __init__(self, vectorstore: ChromaVectorStore):
        self.vectorstore = vectorstore

    def search(
        self,
        query: str,
        top_k: int,
        filter: MetadataFilter | None = None,
    ) -> list[RetrievedChunk]:
        where = filter.to_chroma_where() if filter else None
        embedding = self.vectorstore.embedding_provider.embed([query])[0]
        result = self.vectorstore.query_embedding(
            embedding, top_k=top_k, where=where, filter=filter
        )

        ids = (result.get("ids") or [[]])[0]
        docs = (result.get("documents") or [[]])[0]
        metas = (result.get("metadatas") or [[]])[0]
        dists = (result.get("distances") or [[]])[0]

        chunks: list[RetrievedChunk] = []
        for rank, (chunk_id, text, meta, dist) in enumerate(
            zip(ids, docs, metas, dists, strict=False), start=1
        ):
            chunks.append(
                RetrievedChunk(
                    chunk_id=chunk_id,
                    text=text or "",
                    metadata=dict(meta or {}),
                    dense_score=1.0 - float(dist),
                    dense_rank=rank,
                )
            )
        return chunks
