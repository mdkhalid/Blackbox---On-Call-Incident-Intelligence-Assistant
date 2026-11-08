"""Re-ranking: a second scoring pass over the fused candidate list.

Three providers are supported, chosen by config (`retrieval.rerank_provider`):

* ``cross_encoder`` — `sentence-transformers` CrossEncoder. The textbook choice: it scores
  (query, passage) jointly instead of comparing independent embeddings. Requires torch,
  so it is not available in every environment.
* ``lexical``       — an IDF-weighted coverage + proximity + identifier scorer implemented
  here. No model download, deterministic, and it targets exactly what fusion blurs:
  whether the chunk actually contains the query's *specific* tokens in proximity, rather
  than merely ranking near it in either list.
* ``llm``           — an injected callable scores relevance, for LLM-as-reranker setups.

`auto` prefers a cross-encoder and falls back to lexical when the dependency is missing.
Whatever is used, the eval harness reports rerank impact with real numbers.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable

from blackbox.retrieval.filters import MetadataFilter
from blackbox.retrieval.models import RetrievedChunk
from blackbox.retrieval.sparse import tokenize

_PHRASE_SPLIT = re.compile(r"[^a-z0-9_]+")
_IDENTIFIER = re.compile(r"[A-Za-z0-9_]*[_0-9][A-Za-z0-9_]*")

# Rerank score weights. Kept explicit so a sweep can justify them like the fusion weights.
W_COVERAGE = 0.55
W_PROXIMITY = 0.20
W_IDENTIFIER = 0.25

# Function words that carry no retrieval signal. Without removing these, a query like
# "the thing where requests pile up" scores every chunk highly for containing "the".
_FUNCTION_WORDS = (
    "a an the and or but if then than that this these those there here"
    " is are was were be been being am"
    " do does did doing done"
    " have has had having"
    " of in on at to for from by with without into over under about after before"
    " as it its we our us they them their he she his her you your i me my"
    " not no nor so such very too also just only even still yet"
    " what which who whom whose when where why how"
    " can could should would may might must will shall"
    " up down out off again further once more most some any all each both few many much"
    " thing things something anything everything nothing"
)
STOPWORDS = frozenset(_FUNCTION_WORDS.split())


def _norm_text(text: str) -> str:
    return " ".join(_PHRASE_SPLIT.split(text.lower()))


def _tiebreak(chunk: RetrievedChunk) -> tuple[int, str]:
    """Equal rerank scores fall back to the fused order, then chunk id for determinism."""
    return (chunk.fused_rank if chunk.fused_rank is not None else 10**6, chunk.chunk_id)


def _order(scored: list[tuple[float, RetrievedChunk]], top_k: int) -> list[RetrievedChunk]:
    scored.sort(key=lambda pair: (-pair[0], _tiebreak(pair[1])))
    out: list[RetrievedChunk] = []
    for rank, (score, chunk) in enumerate(scored[:top_k], start=1):
        chunk.rerank_score = round(float(score), 6)
        chunk.rerank_rank = rank
        out.append(chunk)
    return out


class LexicalReranker:
    """Model-free cross-encoder substitute.

    Three signals, all measured against the query rather than the corpus ranking:

    1. ``coverage``   — IDF-weighted share of *content* query terms present in the chunk.
       Rare terms (error codes, `p99`) dominate.
    2. ``proximity``  — how tightly the matched terms cluster. A chunk containing
       "connection pool exhausted" should beat one that says "connection" in paragraph one
       and "exhausted" in paragraph nine.
    3. ``identifier`` — exact hits on code-like query tokens (`ERR_*`, `p99`, `checkout-service`),
       which is the failure mode dense retrieval has by construction.

    Abstention is the important part. Content terms are query tokens that are neither
    stopwords nor present in most of the corpus; if a query has no such term
    ("the thing where requests pile up"), every chunk scores 0.0, the tie-break falls back
    to the fused order, and the reranker has declined to reorder. Without this, such a query
    matches "the" in every document and the reranker scrambles a good fused ranking, which
    the eval harness measures as a real MRR loss.
    """

    def __init__(self, idf: dict[str, float] | None = None, *, doc_freq: dict[str, int] | None = None, n_docs: int = 0):
        self._idf = idf or {}
        self._doc_freq = doc_freq or {}
        self._n_docs = n_docs

    @classmethod
    def from_corpus(cls, texts: list[str]) -> LexicalReranker:
        docs = [set(tokenize(t)) for t in texts]
        n = len(docs) or 1
        df: Counter[str] = Counter()
        for tokens in docs:
            df.update(tokens)
        idf = {term: math.log((n + 1) / (freq + 0.5)) + 1.0 for term, freq in df.items()}
        return cls(idf, doc_freq=dict(df), n_docs=n)

    def _idf_of(self, term: str) -> float:
        # Unknown terms are maximally discriminative on purpose: an unseen error code is
        # the strongest possible signal that this chunk is about it.
        if not self._idf:
            return 1.0
        return self._idf.get(term, max(self._idf.values()))

    def _is_content_term(self, term: str) -> bool:
        if term in STOPWORDS:
            return False
        if not self._n_docs:
            return True
        # A term present in most documents cannot distinguish between them.
        return self._doc_freq.get(term, 0) <= self._n_docs / 2

    def content_terms(self, query: str) -> list[str]:
        return [t for t in dict.fromkeys(tokenize(query)) if self._is_content_term(t)]

    def score(self, query: str, chunk: RetrievedChunk) -> float:
        terms = self.content_terms(query)
        if not terms:
            # No informative term: abstain so the fused ranking survives untouched.
            return 0.0

        text_lower = chunk.text.lower()
        chunk_tokens = tokenize(chunk.text)
        if not chunk_tokens:
            return 0.0
        present = set(chunk_tokens)

        total_weight = sum(self._idf_of(t) for t in terms) or 1.0
        matched = [t for t in terms if t in present]
        if not matched:
            return 0.0
        coverage = sum(self._idf_of(t) for t in matched) / total_weight

        proximity = 0.0
        if len(matched) > 1:
            positions = sorted(
                min((i for i, tok in enumerate(chunk_tokens) if tok == t), default=-1)
                for t in matched
            )
            positions = [p for p in positions if p >= 0]
            if len(positions) > 1:
                span = positions[-1] - positions[0] + 1
                # 1.0 when adjacent, decaying with the span the matches are spread over.
                proximity = len(positions) / span

        identifiers = [t for t in terms if _IDENTIFIER.match(t)]
        if identifiers:
            hits = sum(1 for t in identifiers if t in text_lower or t in present)
            identifier = hits / len(identifiers)
        else:
            identifier = 0.0

        return (
            W_COVERAGE * coverage + W_PROXIMITY * proximity + W_IDENTIFIER * identifier
        )

    def rerank(
        self,
        query: str,
        chunks: list[RetrievedChunk],
        top_k: int,
        filter: MetadataFilter | None = None,
    ) -> list[RetrievedChunk]:
        scored = [(self.score(query, c), c) for c in chunks]
        return _order(scored, top_k)


class CrossEncoderReranker:
    """`sentence-transformers` CrossEncoder wrapper, loaded lazily."""

    def __init__(self, model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"):
        try:
            from sentence_transformers import CrossEncoder
        except ImportError as e:  # pragma: no cover - depends on environment
            raise ImportError(
                "cross-encoder reranking needs sentence-transformers + torch; "
                "set retrieval.rerank_provider to 'lexical' or install the 'llm' extra"
            ) from e
        self.model_name = model_name
        self.model = CrossEncoder(model_name)

    def rerank(
        self,
        query: str,
        chunks: list[RetrievedChunk],
        top_k: int,
        filter: MetadataFilter | None = None,
    ) -> list[RetrievedChunk]:
        if not chunks:
            return []
        pairs = [(query, c.text) for c in chunks]
        scores = self.model.predict(pairs)
        scored = [(float(score), chunk) for score, chunk in zip(scores, chunks, strict=False)]
        return _order(scored, top_k)


class LLMReranker:
    """LLM-as-reranker: the injected callable returns a relevance score per chunk."""

    def __init__(self, scorer: Callable[[str, list[RetrievedChunk]], list[float]]):
        self._scorer = scorer

    def rerank(
        self,
        query: str,
        chunks: list[RetrievedChunk],
        top_k: int,
        filter: MetadataFilter | None = None,
    ) -> list[RetrievedChunk]:
        if not chunks:
            return []
        scores = self._scorer(query, chunks)
        scored = [(float(score), chunk) for score, chunk in zip(scores, chunks, strict=False)]
        return _order(scored, top_k)


class NullReranker:
    """Explicit no-op so the "without rerank" comparison uses the same code path."""

    def rerank(
        self,
        query: str,
        chunks: list[RetrievedChunk],
        top_k: int,
        filter: MetadataFilter | None = None,
    ) -> list[RetrievedChunk]:
        return chunks[:top_k]


def build_reranker(
    provider: str,
    *,
    model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2",
    corpus_texts: list[str] | None = None,
    llm_scorer: Callable[[str, list[RetrievedChunk]], list[float]] | None = None,
    warn: Callable[[str], None] | None = None,
):
    """Resolve a reranker from the configured provider name.

    `auto` silently degrades to lexical when torch is absent, which is the common case for
    a CPU-only laptop; the eval reports which reranker actually ran.
    """
    choice = (provider or "auto").lower()
    if choice == "none":
        return NullReranker()
    if choice == "lexical":
        return LexicalReranker.from_corpus(corpus_texts or [])
    if choice == "cross_encoder":
        return CrossEncoderReranker(model_name)
    if choice == "llm":
        if llm_scorer is None:
            raise ValueError("rerank_provider='llm' requires an llm_scorer callable")
        return LLMReranker(llm_scorer)
    if choice == "auto":
        try:
            return CrossEncoderReranker(model_name)
        except ImportError:
            if warn:
                warn("cross-encoder unavailable (no torch); using lexical reranker")
            return LexicalReranker.from_corpus(corpus_texts or [])
    raise ValueError(f"unknown rerank provider: {provider}")
