import pytest

from blackbox.retrieval.fusion import FusionConfig, fuse
from blackbox.retrieval.models import RetrievedChunk


def _chunk(cid, dense=None, sparse=None, dense_rank=None, sparse_rank=None):
    return RetrievedChunk(
        chunk_id=cid,
        text=f"text {cid}",
        metadata={"doc_id": cid},
        dense_score=dense,
        sparse_score=sparse,
        dense_rank=dense_rank,
        sparse_rank=sparse_rank,
    )


def test_rrf_exact_formula():
    dense = [_chunk("a", dense=0.9, dense_rank=1), _chunk("b", dense=0.8, dense_rank=2)]
    sparse = [_chunk("b", sparse=10.0, sparse_rank=1), _chunk("a", sparse=5.0, sparse_rank=2)]
    fused = fuse(dense, sparse, FusionConfig(method="rrf", k=60, dense_weight=0.5, sparse_weight=0.5), top_k=5)

    by_id = {c.chunk_id: c for c in fused}
    assert by_id["a"].fused_score == pytest.approx(0.5 / 61 + 0.5 / 62)
    assert by_id["b"].fused_score == pytest.approx(0.5 / 62 + 0.5 / 61)
    # Equal fused score -> deterministic tie-break by chunk id.
    assert [c.chunk_id for c in fused] == ["a", "b"]


def test_rrf_weights_shift_ranking():
    dense = [_chunk("dense-first", dense=0.9, dense_rank=1)]
    sparse = [_chunk("sparse-first", sparse=9.0, sparse_rank=1)]
    dense_heavy = fuse(
        dense, sparse, FusionConfig(method="rrf", k=60, dense_weight=0.9, sparse_weight=0.1), top_k=5
    )
    sparse_heavy = fuse(
        dense, sparse, FusionConfig(method="rrf", k=60, dense_weight=0.1, sparse_weight=0.9), top_k=5
    )
    assert dense_heavy[0].chunk_id == "dense-first"
    assert sparse_heavy[0].chunk_id == "sparse-first"


def test_weighted_fusion_normalizes_within_list():
    dense = [_chunk("a", dense=0.9, dense_rank=1), _chunk("b", dense=0.5, dense_rank=2)]
    sparse = [_chunk("b", sparse=100.0, sparse_rank=1)]
    fused = fuse(
        dense, sparse, FusionConfig(method="weighted", dense_weight=0.5, sparse_weight=0.5), top_k=5
    )
    by_id = {c.chunk_id: c for c in fused}
    # Dense min-max: a=1.0, b=0.0. Sparse has one value -> 1.0. b = 0.5*1.0 = 0.5.
    assert by_id["a"].fused_score == pytest.approx(0.5)
    assert by_id["b"].fused_score == pytest.approx(0.5)


def test_fusion_merges_duplicate_chunks_and_keeps_both_scores():
    dense = [_chunk("shared", dense=0.9, dense_rank=1)]
    sparse = [_chunk("shared", sparse=12.0, sparse_rank=1)]
    fused = fuse(dense, sparse, FusionConfig(), top_k=5)
    assert len(fused) == 1
    assert fused[0].dense_score == 0.9
    assert fused[0].sparse_score == 12.0
    assert fused[0].dense_rank == 1
    assert fused[0].sparse_rank == 1


def test_fusion_handles_chunk_present_in_only_one_list():
    fused = fuse(
        [_chunk("only-dense", dense=0.7, dense_rank=1)], [], FusionConfig(), top_k=5
    )
    assert len(fused) == 1
    assert fused[0].sparse_score is None


def test_fusion_assigns_sequential_ranks_and_truncates():
    dense = [_chunk(f"c{i}", dense=1.0 - i / 100, dense_rank=i + 1) for i in range(10)]
    fused = fuse(dense, [], FusionConfig(), top_k=3)
    assert len(fused) == 3
    assert [c.fused_rank for c in fused] == [1, 2, 3]


def test_empty_inputs_fuse_to_empty():
    assert fuse([], [], FusionConfig(), top_k=5) == []


def test_is_deterministic():
    dense = [_chunk("a", dense=0.9, dense_rank=1), _chunk("b", dense=0.5, dense_rank=2)]
    sparse = [_chunk("b", sparse=3.0, sparse_rank=1), _chunk("a", sparse=2.0, sparse_rank=2)]
    runs = [
        [c.chunk_id for c in fuse(dense, sparse, FusionConfig(), 5)] for _ in range(3)
    ]
    assert runs[0] == runs[1] == runs[2]
