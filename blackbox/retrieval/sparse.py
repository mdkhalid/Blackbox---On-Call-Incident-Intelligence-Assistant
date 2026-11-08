"""Sparse retrieval: in-process BM25 over the chunk corpus.

BM25 exists here for the reason the assignment calls out: embeddings blur exact tokens.
`ERR_POOL_EXHAUSTED`, `p99`, `checkout-service` must match literally, and a lexical model
on the same corpus does that natively.

The index is built from the vector store (the single source of truth for chunks) and
cached until explicitly invalidated after ingestion.
"""
from __future__ import annotations

import re
import threading

from blackbox.ingestion.vectorstore import ChromaVectorStore
from blackbox.retrieval.filters import MetadataFilter
from blackbox.retrieval.models import RetrievedChunk

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def tokenize(text: str) -> list[str]:
    """Tokenize while preserving the distinction between names and identifiers.

    * Hyphens split (`checkout-service` -> `checkout`, `service`) because people write
      service names both ways.
    * camelCase splits (`CheckoutAPI` -> `checkout`, `api`) for the same reason.
    * Underscored identifiers stay whole (`ERR_POOL_EXHAUSTED` is one token).

    That last rule is deliberate. Splitting error codes into parts makes `ERR_AUTH_FAILED`
    and `ERR_AUTH_TIMEOUT` match each other through the shared `err`/`auth` terms, which is
    exactly the false positive this retriever exists to avoid. Prose still bridges the gap:
    a query about "pool exhaustion" matches the sentence around the code, not the code
    token itself.
    """
    tokens: list[str] = []
    for raw in _TOKEN_RE.findall(text):
        lowered = raw.lower()
        tokens.append(lowered)
        if "_" in raw:
            # Keep the identifier intact; BM25 weighs whole identifiers by their rarity.
            continue
        parts = _CAMEL_RE.split(raw)
        if len(parts) > 1:
            tokens.extend(p.lower() for p in parts if p)
    return tokens


class SparseRetriever:
    def __init__(self, vectorstore: ChromaVectorStore):
        self.vectorstore = vectorstore
        self._lock = threading.Lock()
        self._ids: list[str] = []
        self._texts: list[str] = []
        self._metas: list[dict] = []
        self._bm25 = None
        self._ready = False

    # ---- index ----

    def invalidate(self) -> None:
        with self._lock:
            self._ready = False
            self._bm25 = None
            self._ids, self._texts, self._metas = [], [], []

    def warm_up(self) -> None:
        """Build the index eagerly so the first query does not pay for it."""
        self._ensure_index()

    def _ensure_index(self) -> None:
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            data = self.vectorstore.get_all()
            ids = data.get("ids") or []
            texts = data.get("documents") or []
            metas = data.get("metadatas") or []
            corpus = [tokenize(t or "") for t in texts]
            self._ids = list(ids)
            self._texts = list(texts)
            self._metas = [dict(m or {}) for m in metas]
            if corpus:
                from rank_bm25 import BM25Okapi

                self._bm25 = BM25Okapi(corpus)
            else:
                self._bm25 = None
            self._ready = True

    @property
    def size(self) -> int:
        self._ensure_index()
        return len(self._ids)

    def get_chunk(self, chunk_id: str) -> RetrievedChunk | None:
        self._ensure_index()
        try:
            idx = self._ids.index(chunk_id)
        except ValueError:
            return None
        return RetrievedChunk(
            chunk_id=chunk_id, text=self._texts[idx], metadata=dict(self._metas[idx])
        )

    # ---- search ----

    def search(
        self,
        query: str,
        top_k: int,
        filter: MetadataFilter | None = None,
    ) -> list[RetrievedChunk]:
        self._ensure_index()
        if self._bm25 is None:
            return []

        query_tokens = tokenize(query)
        if not query_tokens:
            return []

        scores = self._bm25.get_scores(query_tokens)

        # Metadata scoping happens before ranking, so the returned top-k is drawn from the
        # filtered candidate set rather than the whole corpus.
        candidates: list[tuple[int, float]] = []
        for i, score in enumerate(scores):
            if score <= 0.0:
                continue
            if filter is not None and not filter.matches(self._metas[i]):
                continue
            candidates.append((i, float(score)))

        candidates.sort(key=lambda pair: pair[1], reverse=True)

        out: list[RetrievedChunk] = []
        for rank, (i, score) in enumerate(candidates[:top_k], start=1):
            out.append(
                RetrievedChunk(
                    chunk_id=self._ids[i],
                    text=self._texts[i],
                    metadata=dict(self._metas[i]),
                    sparse_score=score,
                    sparse_rank=rank,
                )
            )
        return out
