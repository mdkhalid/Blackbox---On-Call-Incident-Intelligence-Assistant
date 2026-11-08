import pytest

from blackbox.retrieval.models import RetrievedChunk
from blackbox.retrieval.rerank import (
    LexicalReranker,
    NullReranker,
    build_reranker,
)


def _c(cid, text, fused_rank=1):
    return RetrievedChunk(
        chunk_id=cid, text=text, metadata={"doc_id": cid}, fused_rank=fused_rank
    )


@pytest.fixture
def reranker():
    corpus = [
        "connection pool saturation causes ERR_POOL_EXHAUSTED in checkout-service",
        "tls certificate rotation broke auth-service tokens",
        "dashboard shows request latency percentiles",
    ]
    return LexicalReranker.from_corpus(corpus)


def test_rerank_promotes_chunk_containing_the_exact_identifier(reranker):
    relevant = _c("relevant", "the checkout-service pool hit ERR_POOL_EXHAUSTED", fused_rank=2)
    tangential = _c("tangential", "a dashboard shows latency percentiles", fused_rank=1)
    out = reranker.rerank("ERR_POOL_EXHAUSTED checkout-service", [tangential, relevant], top_k=2)
    assert out[0].chunk_id == "relevant"
    assert out[0].rerank_rank == 1


def test_rerank_rewards_proximity_over_scattered_matches(reranker):
    tight = _c("tight", "connection pool saturation", fused_rank=2)
    scattered = _c(
        "scattered",
        "connection " + " ".join(["filler"] * 40) + " pool " + " ".join(["filler"] * 40) + " saturation",
        fused_rank=1,
    )
    out = reranker.rerank("connection pool saturation", [scattered, tight], top_k=2)
    assert out[0].chunk_id == "tight"
    assert out[0].rerank_score > out[1].rerank_score


def test_rerank_scores_are_bounded_and_descending(reranker):
    chunks = [
        _c("a", "ERR_POOL_EXHAUSTED checkout-service connection pool"),
        _c("b", "completely unrelated content about dashboards"),
        _c("c", "connection pool"),
    ]
    out = reranker.rerank("ERR_POOL_EXHAUSTED connection pool", chunks, top_k=3)
    scores = [c.rerank_score for c in out]
    assert scores == sorted(scores, reverse=True)
    assert all(0.0 <= s <= 1.0 for s in scores)
    assert out[0].chunk_id == "a"


def test_rerank_is_stable_on_ties(reranker):
    a = _c("b-chunk", "identical text", fused_rank=1)
    b = _c("a-chunk", "identical text", fused_rank=2)
    out = reranker.rerank("identical", [a, b], top_k=2)
    # Equal rerank scores fall back to fused order, not alphabetical id.
    assert [c.chunk_id for c in out] == ["b-chunk", "a-chunk"]


def test_rerank_truncates_to_top_k(reranker):
    chunks = [_c(f"c{i}", "connection pool", fused_rank=i + 1) for i in range(6)]
    assert len(reranker.rerank("connection pool", chunks, top_k=2)) == 2


def test_rerank_handles_empty_input(reranker):
    assert reranker.rerank("anything", [], top_k=5) == []


def test_rerank_handles_empty_query(reranker):
    out = reranker.rerank("", [_c("a", "connection pool")], top_k=1)
    assert out[0].rerank_score == 0.0


def test_unseen_identifier_is_highly_discriminating(reranker):
    with_code = _c("with", "the error was ERR_NEVER_SEEN_BEFORE")
    without = _c("without", "the error was something else entirely")
    out = reranker.rerank("ERR_NEVER_SEEN_BEFORE", [without, with_code], top_k=2)
    assert out[0].chunk_id == "with"


def test_reranker_abstains_when_query_has_only_stopwords(reranker):
    # Every chunk contains these function words, so the reranker must not reorder anything:
    # abstaining returns all-zero scores and the fused order wins the tie-break.
    chunks = [
        _c("first", "the service was slow and it had a problem", fused_rank=1),
        _c("second", "the service had a problem and it was slow", fused_rank=2),
    ]
    out = reranker.rerank("what is it that they had", chunks, top_k=2)
    assert [c.chunk_id for c in out] == ["first", "second"]
    assert all(c.rerank_score == 0.0 for c in out)


def test_reranker_abstains_on_terms_present_in_most_documents(reranker):
    # "service" appears in every fixture document, so it cannot rank one above another.
    assert "service" not in reranker.content_terms("the service")
    assert reranker.content_terms("ERR_POOL_EXHAUSTED") == ["err_pool_exhausted"]


def test_reranker_does_not_abstain_when_a_rare_term_is_present(reranker):
    chunks = [
        _c("plain", "the service had a problem", fused_rank=1),
        _c("coded", "the service hit ERR_POOL_EXHAUSTED", fused_rank=2),
    ]
    out = reranker.rerank("the service ERR_POOL_EXHAUSTED", chunks, top_k=2)
    assert out[0].chunk_id == "coded"
    assert out[0].rerank_score > 0.0


def test_null_reranker_preserves_order_and_sets_no_score():
    chunks = [_c("a", "x", fused_rank=1), _c("b", "y", fused_rank=2)]
    out = NullReranker().rerank("q", chunks, top_k=5)
    assert [c.chunk_id for c in out] == ["a", "b"]
    assert all(c.rerank_score is None for c in out)


def test_build_reranker_none_provider():
    assert isinstance(build_reranker("none"), NullReranker)


def test_build_reranker_lexical_provider():
    assert isinstance(build_reranker("lexical", corpus_texts=["x"]), LexicalReranker)


def test_build_reranker_auto_falls_back_without_torch():
    messages: list[str] = []
    r = build_reranker("auto", warn=messages.append)
    # Either a cross-encoder (torch present) or the documented lexical fallback.
    assert r is not None
    assert isinstance(r, LexicalReranker) or type(r).__name__ == "CrossEncoderReranker"


def test_build_reranker_llm_requires_scorer():
    with pytest.raises(ValueError):
        build_reranker("llm")


def test_build_reranker_llm_uses_injected_scorer():
    def scorer(query, chunks):
        return [float(i) for i in range(len(chunks))]

    r = build_reranker("llm", llm_scorer=scorer)
    out = r.rerank("q", [_c("a", "x"), _c("b", "y")], top_k=2)
    assert out[0].chunk_id == "b"


def test_build_reranker_rejects_unknown_provider():
    with pytest.raises(ValueError):
        build_reranker("magic")
