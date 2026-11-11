"""Sparse retrieval: in-process BM25 over the chunk corpus.

BM25 exists here for the reason the assignment calls out: embeddings blur exact tokens.
`ERR_POOL_EXHAUSTED`, `p99`, `checkout-service` must match literally, and a lexical model
on the same corpus does that natively.

This is the *development* adapter for the sparse leg. It holds the whole corpus, its
tokenised form and the BM25 index in the pod's memory, and rebuilds all of them after
an ingest. That is acceptable for a corpus of tens of thousands of chunks and not
acceptable at the target scale, so it sits behind the `SparseStore` protocol and can be
replaced by OpenSearch without changing anything above it.

Two properties matter for the request path:

* The index is published atomically. A reader either sees the old index or the new
  one, never a half-built one.
* A rebuild is O(corpus) and does not belong on the request path. `rebuild` is called
  by the ingestion worker; the request path only ever reads an index that exists.
"""
from __future__ import annotations

import math
import re
import threading
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from blackbox.ingestion.vectorstore import ChromaVectorStore
from blackbox.retrieval.filters import MetadataFilter
from blackbox.retrieval.models import RetrievedChunk

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

# Sorts after every real document id, so a chunk without one lands last. The
# OpenSearch adapter asks for the same with `missing: _last`.
_MISSING_DOC_ID = "￿"


def tokenize(text: str) -> list[str]:
    """Tokenize while preserving the distinction between names and identifiers.

    * Hyphens split (`checkout-service` -> `checkout`, `service`) because people write
      service names both ways.
    * camelCase splits (`CheckoutAPI` -> `checkout`, `api`) for the same reason.
    * Underscored identifiers stay whole (`ERR_POOL_EXHAUSTED` is one token).

    That last rule is deliberate. Splitting error codes into parts makes `ERR_AUTH_FAILED`
    and `ERR_AUTH_TIMEOUT` match each other through the shared `err`/`auth` terms, which
    is exactly the false positive this retriever exists to avoid. Prose still bridges the gap:
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


@dataclass(slots=True)
class _Corpus:
    """An immutable snapshot of the corpus and its BM25 index."""

    ids: list[str]
    texts: list[str]
    metas: list[dict]
    by_id: dict[str, int]
    bm25: Any


def _doc_id(metadata: dict) -> str:
    """The document id, or a sentinel that sorts last."""
    value = (metadata or {}).get("doc_id")
    return str(value) if value else _MISSING_DOC_ID


class BM25Index:
    """Lucene's BM25, computed locally.

    `rank_bm25` is a different implementation of the same idea: it uses
    `log((N - df + 0.5) / (df + 0.5))` where Lucene uses
    `log(1 + (N - df + 0.5) / (df + 0.5))`, and it replaces a negative inverse
    document frequency with an epsilon. The two rank the same corpus slightly
    differently.

    That difference matters here. The OpenSearch adapter scores with Lucene, so an
    adapter built on `rank_bm25` was not measuring the system the production backend
    actually runs, and every measurement taken against it was a measurement of
    something else. Computing Lucene's formula locally makes the two adapters produce
    the same scores, which is what lets the migration be shown to be
    behaviour-preserving rather than merely close.
    """

    def __init__(
        self, corpus: list[list[str]], *, k1: float = 1.5, b: float = 0.75
    ) -> None:
        self.k1 = k1
        self.b = b
        self.doc_count = len(corpus)
        self.lengths: list[int] = []
        postings: dict[str, list[tuple[int, int]]] = {}
        doc_freqs: Counter[str] = Counter()
        for index, tokens in enumerate(corpus):
            self.lengths.append(len(tokens))
            counts = Counter(tokens)
            for term, freq in counts.items():
                postings.setdefault(term, []).append((index, freq))
                doc_freqs[term] += 1
        self.postings = postings
        self.total_length = sum(self.lengths)
        self.avg_length = (self.total_length / self.doc_count) if self.doc_count else 0.0
        # Lucene's inverse document frequency. It is always positive, which is one of
        # the places `rank_bm25` differs.
        self.idf = {
            term: math.log(1.0 + (self.doc_count - df + 0.5) / (df + 0.5))
            for term, df in doc_freqs.items()
        }

    def scores(self, query_terms: list[str]) -> list[float]:
        """The BM25 score of every document for the query."""
        out = [0.0] * self.doc_count
        if not self.doc_count or not query_terms or not self.avg_length:
            return out
        for term, query_freq in Counter(query_terms).items():
            idf = self.idf.get(term)
            if idf is None:
                continue
            for index, freq in self.postings.get(term, ()):
                length = self.lengths[index]
                norm = 1.0 - self.b + self.b * (length / self.avg_length)
                out[index] += (
                    query_freq
                    * idf
                    * (freq * (self.k1 + 1.0))
                    / (freq + self.k1 * norm)
                )
        return out


class SparseRetriever:
    """BM25 over the chunk corpus, behind the `SparseStore` protocol."""

    def __init__(self, vectorstore: ChromaVectorStore):
        self.vectorstore = vectorstore
        self._lock = threading.Lock()
        self._build_lock = threading.Lock()
        self._corpus: _Corpus | None = None
        self._stale = False

    # ---- index ----

    def invalidate(self) -> None:
        """Mark the corpus-derived state as stale.

        Nothing is rebuilt here. A rebuild is O(corpus), and doing it inside the
        request path would charge the next query for the previous ingest. The
        ingestion worker calls `rebuild` instead.
        """
        with self._lock:
            self._stale = True

    def rebuild(self) -> None:
        """Rebuild the index from the corpus and publish it atomically."""
        corpus = self._build()
        with self._lock:
            self._corpus = corpus
            self._stale = False

    def warm_up(self) -> None:
        """Make sure an index exists, building it if this is a cold start.

        This is the development fallback: with no ingestion worker running, the first
        query has to build the index itself.
        """
        self._current()

    def _current(self) -> _Corpus:
        with self._lock:
            if self._corpus is not None and not self._stale:
                return self._corpus
        # Only one thread builds the index; the others wait for it and then use the
        # published snapshot.
        with self._build_lock:
            with self._lock:
                if self._corpus is not None and not self._stale:
                    return self._corpus
            corpus = self._build()
            with self._lock:
                self._corpus = corpus
                self._stale = False
            return corpus

    def _build(self) -> _Corpus:
        data = self.vectorstore.get_all()
        ids = list(data.get("ids") or [])
        texts = list(data.get("documents") or [])
        metas = [dict(m or {}) for m in (data.get("metadatas") or [])]
        by_id = {chunk_id: index for index, chunk_id in enumerate(ids)}

        bm25 = BM25Index([tokenize(text or "") for text in texts]) if ids else None
        return _Corpus(ids=ids, texts=texts, metas=metas, by_id=by_id, bm25=bm25)

    @property
    def size(self) -> int:
        return len(self._current().ids)

    def get_chunk(self, chunk_id: str) -> RetrievedChunk | None:
        corpus = self._current()
        index = corpus.by_id.get(chunk_id)
        if index is None:
            return None
        return RetrievedChunk(
            chunk_id=chunk_id,
            text=corpus.texts[index],
            metadata=dict(corpus.metas[index]),
        )

    # ---- write path ----

    def upsert_chunks(self, chunks: Iterable[Any]) -> int:
        """Accept a write; the corpus itself comes from the dense store.

        This adapter derives its corpus from the vector store, which the ingestion
        pipeline writes to first, so there is nothing to store here. The write only
        marks the index stale, and the next `rebuild` picks up the new corpus. The
        OpenSearch adapter, which owns its index, really does store the chunks.
        """
        count = sum(1 for _ in chunks)
        if count:
            self.invalidate()
        return count

    def delete_by_doc(self, doc_id: str) -> None:
        """Accept a delete; the corpus itself comes from the dense store."""
        self.invalidate()

    # ---- search ----

    def search(
        self,
        query: str,
        top_k: int,
        filter: MetadataFilter | None = None,
    ) -> list[RetrievedChunk]:
        corpus = self._current()
        if corpus.bm25 is None:
            return []

        query_tokens = tokenize(query)
        if not query_tokens:
            return []

        scores = corpus.bm25.scores(query_tokens)

        # Metadata scoping happens before ranking, so the returned top-k is drawn from the
        # filtered candidate set rather than the whole corpus.
        candidates: list[tuple[int, float]] = []
        for i, score in enumerate(scores):
            if score <= 0.0:
                continue
            if filter is not None and not filter.matches(corpus.metas[i]):
                continue
            candidates.append((i, float(score)))

        # Equal scores are broken by document id and then by chunk id. Both come from
        # the chunk itself, so the ranking does not depend on the dense store's
        # internal id order, and the OpenSearch adapter can compute the same order.
        candidates.sort(
            key=lambda pair: (
                -pair[1],
                _doc_id(corpus.metas[pair[0]]),
                corpus.ids[pair[0]],
            )
        )

        out: list[RetrievedChunk] = []
        for rank, (i, score) in enumerate(candidates[:top_k], start=1):
            out.append(
                RetrievedChunk(
                    chunk_id=corpus.ids[i],
                    text=corpus.texts[i],
                    metadata=dict(corpus.metas[i]),
                    sparse_score=score,
                    sparse_rank=rank,
                )
            )
        return out
