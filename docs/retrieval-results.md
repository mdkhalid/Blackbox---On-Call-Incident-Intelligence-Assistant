# Retrieval Results — Blackbox

To be populated by `eval/retrieval_eval.py` run. Format below.

## Comparison Table (placeholder)

| Strategy | Precision@5 | Recall@5 | MRR | Notes |
|---|---|---|---|---|
| Dense-only | TBD | TBD | TBD | fails error-code queries |
| Sparse-only (BM25) | TBD | TBD | TBD | fails semantic queries |
| Hybrid (RRF) | TBD | TBD | TBD | config weights |
| Hybrid + Rerank | TBD | TBD | TBD | measured with/without |

## Fusion Weights
Config-driven; justified by eval numbers above (not intuition).

## Reranking Impact
Reported with evidence; dropped if no gain.

## Groundedness
Fraction of answer claims supported by retrieved context (see `eval/groundedness_eval.py`).

## Reproduction
`python -m eval.retrieval_eval` — results written here.
