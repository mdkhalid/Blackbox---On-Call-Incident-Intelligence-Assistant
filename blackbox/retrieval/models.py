"""Retrieval result models shared by dense, sparse, fusion and rerank stages."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from langchain_core.documents import Document


@dataclass
class RetrievedChunk:
    """A single chunk with the score trail from every retrieval stage.

    Each stage records both its raw score and its rank inside that stage; the trace
    viewer and the eval harness both need the trail, not just the final list.
    """

    chunk_id: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)
    dense_score: float | None = None
    dense_rank: int | None = None
    sparse_score: float | None = None
    sparse_rank: int | None = None
    fused_score: float | None = None
    fused_rank: int | None = None
    rerank_score: float | None = None
    rerank_rank: int | None = None

    @property
    def score(self) -> float:
        """Best available score, most specific stage first."""
        for value in (self.rerank_score, self.fused_score, self.dense_score):
            if value is not None:
                return value
        return 0.0

    def to_document(self) -> Document:
        md = dict(self.metadata)
        md.update(
            {
                "chunk_id": self.chunk_id,
                "dense_score": self.dense_score,
                "dense_rank": self.dense_rank,
                "sparse_score": self.sparse_score,
                "sparse_rank": self.sparse_rank,
                "fused_score": self.fused_score,
                "fused_rank": self.fused_rank,
                "rerank_score": self.rerank_score,
                "rerank_rank": self.rerank_rank,
            }
        )
        return Document(page_content=self.text, metadata=md)

    def score_trail(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "doc_id": self.metadata.get("doc_id"),
            "section": self.metadata.get("section"),
            "doc_type": self.metadata.get("doc_type"),
            "dense_score": self.dense_score,
            "dense_rank": self.dense_rank,
            "sparse_score": self.sparse_score,
            "sparse_rank": self.sparse_rank,
            "fused_score": self.fused_score,
            "fused_rank": self.fused_rank,
            "rerank_score": self.rerank_score,
            "rerank_rank": self.rerank_rank,
        }


def to_documents(chunks: list[RetrievedChunk]) -> list[Document]:
    return [c.to_document() for c in chunks]
