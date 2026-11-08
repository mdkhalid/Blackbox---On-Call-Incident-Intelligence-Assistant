"""Retrieval quality metrics.

Document-level grading: a retrieved chunk counts as a hit when its `doc_id` appears in the
question's `relevant_doc_ids`. Chunk-level grading would reward a retriever for returning
ten chunks from the right document, which says nothing about whether it found the document.

Why these three metrics:

* ``MRR`` — the primary metric for an on-call assistant. The engineer reads the top answer;
  if the right document is at rank 9 they will not see it. MRR is sensitive to exactly that.
* ``recall@k`` — "did we surface every document that matters". Matters for the
  "has this happened before" question, which often has several prior incidents.
* ``precision@k`` — guards against padding the list with near-misses, which for this
  product is actively harmful (a confident wrong precedent at 2 AM).

Reported together because any single one is gameable.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class QueryResult:
    question_id: str
    category: str
    retrieved_doc_ids: list[str]
    relevant_doc_ids: list[str]
    latency_ms: float = 0.0

    @property
    def reciprocal_rank(self) -> float:
        for rank, doc_id in enumerate(self.retrieved_doc_ids, start=1):
            if doc_id in self.relevant_doc_ids:
                return 1.0 / rank
        return 0.0


@dataclass
class Metrics:
    count: int = 0
    mrr: float = 0.0
    recall: float = 0.0
    precision: float = 0.0
    hit_rate: float = 0.0
    mean_latency_ms: float = 0.0
    per_category: dict[str, dict[str, float]] = field(default_factory=dict)


def precision_at_k(retrieved: list[str], relevant: list[str], k: int) -> float:
    if k <= 0:
        return 0.0
    top = retrieved[:k]
    if not top:
        return 0.0
    return sum(1 for d in top if d in relevant) / len(top)


def recall_at_k(retrieved: list[str], relevant: list[str], k: int) -> float:
    if not relevant:
        return 0.0
    top = set(retrieved[:k])
    return sum(1 for d in relevant if d in top) / len(relevant)


def reciprocal_rank(retrieved: list[str], relevant: list[str]) -> float:
    for rank, doc_id in enumerate(retrieved, start=1):
        if doc_id in relevant:
            return 1.0 / rank
    return 0.0


def _dedupe(doc_ids: list[str]) -> list[str]:
    """Collapse doc ids so one document cannot occupy many ranks.

    Without this, a retriever that returns six chunks of the same postmortem scores the
    same as one that finds six distinct relevant documents.
    """
    seen: list[str] = []
    for doc_id in doc_ids:
        if doc_id and doc_id not in seen:
            seen.append(doc_id)
    return seen


def summarize(results: list[QueryResult], *, k: int) -> Metrics:
    if not results:
        return Metrics()

    mrr = sum(r.reciprocal_rank for r in results) / len(results)
    recall = sum(recall_at_k(r.retrieved_doc_ids, r.relevant_doc_ids, k) for r in results) / len(
        results
    )
    precision = sum(
        precision_at_k(r.retrieved_doc_ids, r.relevant_doc_ids, k) for r in results
    ) / len(results)
    hit_rate = sum(1 for r in results if r.reciprocal_rank > 0) / len(results)
    latency = sum(r.latency_ms for r in results) / len(results)

    by_cat: dict[str, list[QueryResult]] = {}
    for r in results:
        by_cat.setdefault(r.category, []).append(r)

    per_category: dict[str, dict[str, float]] = {}
    for category, items in sorted(by_cat.items()):
        per_category[category] = {
            "n": float(len(items)),
            "mrr": round(sum(r.reciprocal_rank for r in items) / len(items), 4),
            "recall": round(
                sum(recall_at_k(r.retrieved_doc_ids, r.relevant_doc_ids, k) for r in items)
                / len(items),
                4,
            ),
            "precision": round(
                sum(precision_at_k(r.retrieved_doc_ids, r.relevant_doc_ids, k) for r in items)
                / len(items),
                4,
            ),
        }

    return Metrics(
        count=len(results),
        mrr=round(mrr, 4),
        recall=round(recall, 4),
        precision=round(precision, 4),
        hit_rate=round(hit_rate, 4),
        mean_latency_ms=round(latency, 2),
        per_category=per_category,
    )


def doc_ids_from_chunks(chunks) -> list[str]:
    """Extract de-duplicated doc ids in retrieval order."""
    return _dedupe([c.metadata.get("doc_id") for c in chunks])
