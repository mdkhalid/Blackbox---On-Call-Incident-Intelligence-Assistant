from blackbox.graph.grounding import (
    strip_unsupported,
    strip_unsupported_sentences,
    verify_answer,
    verify_claims,
)

CHUNKS = [
    {
        "chunk_id": "c1",
        "doc_id": "pm-001",
        "section": "Root Cause",
        "text": (
            "The root cause was ERR_POOL_EXHAUSTED in checkout-service. "
            "The pool was saturated."
        ),
    }
]


def test_supported_claim_matches_its_source():
    result = verify_claims(
        [{"text": "ERR_POOL_EXHAUSTED in checkout-service", "sources": ["c1"]}],
        CHUNKS,
        min_overlap=0.3,
    )
    assert result.verdicts[0].supported is True
    assert result.supported_count == 1


def test_claim_citing_missing_source_is_unsupported():
    result = verify_claims(
        [{"text": "ERR_POOL_EXHAUSTED in checkout-service", "sources": ["c9"]}],
        CHUNKS,
        min_overlap=0.3,
    )
    assert result.verdicts[0].supported is False


def test_claim_without_source_is_traced_to_best_chunk():
    result = verify_claims(
        [{"text": "The pool was saturated", "sources": []}], CHUNKS, min_overlap=0.3
    )
    assert result.verdicts[0].supported is True
    assert result.verdicts[0].matched_chunk_ids == ["c1"]


def test_unrelated_claim_is_unsupported():
    result = verify_claims(
        [{"text": "The moon is made of cheese", "sources": []}], CHUNKS, min_overlap=0.3
    )
    assert result.verdicts[0].supported is False


def test_strip_unsupported_removes_the_sentence():
    result = verify_claims(
        [
            {"text": "The pool was saturated.", "sources": ["c1"]},
            {"text": "The moon is made of cheese.", "sources": []},
        ],
        CHUNKS,
        min_overlap=0.3,
    )
    stripped = strip_unsupported(
        "The pool was saturated. The moon is made of cheese.", result
    )
    assert "moon" not in stripped.lower()
    assert "saturated" in stripped.lower()


def test_answer_prose_is_verified_even_when_it_is_not_a_declared_claim():
    """Fabricated prose that the generator never listed as a claim is still checked."""
    result = verify_answer(
        "The pool was saturated. NASA faked the moon landing in 1969.",
        CHUNKS,
        min_overlap=0.3,
    )
    assert result.supported_count == 1
    stripped = strip_unsupported_sentences(
        "The pool was saturated. NASA faked the moon landing in 1969.", result
    )
    assert "saturated" in stripped.lower()
    assert "moon" not in stripped.lower()


def test_a_sentence_with_a_citation_is_checked_against_the_cited_chunk():
    result = verify_answer(
        "The pool was saturated [c1]. Unrelated prose [c1].",
        CHUNKS,
        min_overlap=0.3,
    )
    assert [verdict.supported for verdict in result.verdicts] == [True, False]
