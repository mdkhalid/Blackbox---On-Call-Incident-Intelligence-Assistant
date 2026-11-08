import pytest

from blackbox.eval.metrics import (
    QueryResult,
    doc_ids_from_chunks,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
    summarize,
)


def test_reciprocal_rank_first_hit():
    assert reciprocal_rank(["a", "b", "c"], ["a"]) == 1.0


def test_reciprocal_rank_second_hit():
    assert reciprocal_rank(["x", "b", "c"], ["b"]) == 0.5


def test_reciprocal_rank_no_hit():
    assert reciprocal_rank(["x", "y"], ["z"]) == 0.0


def test_precision_at_k_counts_only_top_k():
    assert precision_at_k(["a", "x", "b"], ["a", "b"], k=2) == 0.5
    assert precision_at_k(["a", "x", "b"], ["a", "b"], k=3) == pytest.approx(2 / 3)


def test_precision_with_empty_results():
    assert precision_at_k([], ["a"], k=5) == 0.0


def test_precision_uses_returned_count_not_k():
    # Returning 1 perfect result should not be penalised for not filling k.
    assert precision_at_k(["a"], ["a"], k=5) == 1.0


def test_recall_at_k():
    assert recall_at_k(["a", "b"], ["a", "b", "c"], k=5) == pytest.approx(2 / 3)
    assert recall_at_k(["a", "x"], ["a", "b"], k=5) == 0.5


def test_recall_with_empty_relevant_set_is_zero():
    assert recall_at_k(["a"], [], k=5) == 0.0


def test_recall_respects_k():
    assert recall_at_k(["x", "y", "a"], ["a"], k=2) == 0.0


def test_summarize_averages_across_questions():
    results = [
        QueryResult("q1", "exact_token", ["a"], ["a"], latency_ms=10),
        QueryResult("q2", "exact_token", ["x", "b"], ["b"], latency_ms=20),
        QueryResult("q3", "structural", ["x"], ["z"], latency_ms=30),
    ]
    m = summarize(results, k=5)
    assert m.count == 3
    assert m.mrr == pytest.approx((1.0 + 0.5 + 0.0) / 3, abs=1e-4)
    assert m.mean_latency_ms == pytest.approx(20.0, abs=0.01)
    assert m.hit_rate == pytest.approx(2 / 3, abs=1e-4)


def test_summarize_per_category_breakdown():
    results = [
        QueryResult("q1", "exact_token", ["a"], ["a"]),
        QueryResult("q2", "paraphrase", ["x"], ["z"]),
    ]
    m = summarize(results, k=5)
    assert m.per_category["exact_token"]["mrr"] == 1.0
    assert m.per_category["paraphrase"]["mrr"] == 0.0


def test_summarize_empty_is_zeroed():
    m = summarize([], k=5)
    assert m.count == 0
    assert m.mrr == 0.0


def test_doc_ids_are_deduped_preserving_order():
    class C:
        def __init__(self, doc_id):
            self.metadata = {"doc_id": doc_id}

    chunks = [C("pm-001"), C("pm-001"), C("pm-002"), C("pm-001")]
    # One document must not occupy several ranks.
    assert doc_ids_from_chunks(chunks) == ["pm-001", "pm-002"]


def test_doc_ids_skip_missing_metadata():
    class C:
        def __init__(self, metadata):
            self.metadata = metadata

    assert doc_ids_from_chunks([C({}), C({"doc_id": "pm-1"})]) == ["pm-1"]
