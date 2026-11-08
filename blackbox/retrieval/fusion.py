"""Score fusion for hybrid retrieval.

Two fusions are implemented because the assignment requires the weighting to be
justified with measured numbers rather than intuition:

* ``rrf``      — reciprocal rank fusion. `score = w_dense/(k + rank_dense) + w_sparse/(k + rank_sparse)`.
  Rank-based, so it is immune to dense cosine scores and BM25 scores living on
  incomparable scales. `k` damps the influence of top ranks.
* ``weighted`` — min-max normalise each retriever's scores into [0, 1], then take the
  weighted sum. Keeps within-list score spacing, which RRF throws away.

Both take the weights from config so the eval sweep can change them without code changes.
"""
from __future__ import annotations

from dataclasses import dataclass

from blackbox.retrieval.models import RetrievedChunk


def _merge(base: dict[str, RetrievedChunk], incoming: RetrievedChunk) -> RetrievedChunk:
    existing = base.get(incoming.chunk_id)
    if existing is None:
        base[incoming.chunk_id] = incoming
        return incoming
    # Same chunk from both retrievers: keep text/metadata, take whichever scores are set.
    if existing.dense_score is None and incoming.dense_score is not None:
        existing.dense_score = incoming.dense_score
        existing.dense_rank = incoming.dense_rank
    if existing.sparse_score is None and incoming.sparse_score is not None:
        existing.sparse_score = incoming.sparse_score
        existing.sparse_rank = incoming.sparse_rank
    return existing


@dataclass
class FusionConfig:
    method: str = "rrf"
    k: int = 60
    dense_weight: float = 0.5
    sparse_weight: float = 0.5


def _min_max(scores: list[float | None]) -> dict[int, float]:
    present = [s for s in scores if s is not None]
    if not present:
        return {}
    lo, hi = min(present), max(present)
    if hi == lo:
        # Degenerate spread: every match is equally good.
        return {i: 1.0 for i, s in enumerate(scores) if s is not None}
    return {i: (s - lo) / (hi - lo) for i, s in enumerate(scores) if s is not None}


def fuse(
    dense: list[RetrievedChunk],
    sparse: list[RetrievedChunk],
    config: FusionConfig,
    top_k: int,
) -> list[RetrievedChunk]:
    """Fuse two ranked lists into one, annotating every chunk with its fused rank."""
    pool: dict[str, RetrievedChunk] = {}
    for chunk in dense:
        _merge(pool, chunk)
    for chunk in sparse:
        _merge(pool, chunk)

    chunks = list(pool.values())
    if not chunks:
        return []

    if config.method == "weighted":
        _score_weighted(chunks, config)
    elif config.method == "rrf":
        _score_rrf(chunks, config)
    else:  # pragma: no cover - config validator rejects this
        raise ValueError(f"unknown fusion method: {config.method}")

    chunks.sort(key=lambda c: (-(c.fused_score or 0.0), c.chunk_id))
    for rank, chunk in enumerate(chunks, start=1):
        chunk.fused_rank = rank
    return chunks[:top_k]


def _score_rrf(chunks: list[RetrievedChunk], config: FusionConfig) -> None:
    k = max(config.k, 1)
    for chunk in chunks:
        score = 0.0
        if chunk.dense_rank is not None:
            score += config.dense_weight / (k + chunk.dense_rank)
        if chunk.sparse_rank is not None:
            score += config.sparse_weight / (k + chunk.sparse_rank)
        chunk.fused_score = score


def _score_weighted(chunks: list[RetrievedChunk], config: FusionConfig) -> None:
    dense_norm = _min_max([c.dense_score for c in chunks])
    sparse_norm = _min_max([c.sparse_score for c in chunks])
    for i, chunk in enumerate(chunks):
        score = 0.0
        if i in dense_norm:
            score += config.dense_weight * dense_norm[i]
        if i in sparse_norm:
            score += config.sparse_weight * sparse_norm[i]
        chunk.fused_score = score
