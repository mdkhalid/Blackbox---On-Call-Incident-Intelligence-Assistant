"""Benchmark dense+sparse backend combinations (Phase 2).

Runs the retrieval eval harness against the configured backends and prints a
comparison table. Requires the external stores (OpenSearch, and optionally
Qdrant) to be reachable; combinations whose backend is unavailable are skipped.

Usage:
    python scripts/benchmark_backends.py --k 5
    python scripts/benchmark_backends.py --k 5 --backends chroma+opensearch qdrant+opensearch
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from blackbox.config.settings import get_config
from blackbox.eval.retrieval_eval import build_retriever, run_all
from blackbox.logging_setup import get_logger

logger = get_logger(__name__)


def _run(backend_combo: str, k: int) -> dict | None:
    dense, sparse = backend_combo.split("+")
    cfg = get_config()
    cfg.retrieval.dense_backend = dense
    cfg.retrieval.sparse_backend = sparse
    try:
        retriever = build_retriever(refresh=False)
    except Exception as exc:  # noqa: BLE001
        print(f"  {backend_combo}: unavailable ({exc})")
        return None
    results = run_all(retriever, __import__("json").loads(Path("blackbox/eval/dataset.json").read_text()), k=k)
    hybrid = results.get("hybrid (RRF)")
    if hybrid is None:
        return None
    m, _ = hybrid
    return {"combo": backend_combo, "mrr": m.mrr, "recall": m.recall, "precision": m.precision}


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark backend combinations")
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument(
        "--backends",
        nargs="+",
        default=["chroma+bm25", "chroma+opensearch"],
    )
    args = parser.parse_args()

    print(f"{'combo':<24} {'MRR':>8} {'R@k':>8} {'P@k':>8}")
    for combo in args.backends:
        res = _run(combo, args.k)
        if res is None:
            continue
        print(f"{res['combo']:<24} {res['mrr']:>8.4f} {res['recall']:>8.4f} {res['precision']:>8.4f}")


if __name__ == "__main__":
    main()