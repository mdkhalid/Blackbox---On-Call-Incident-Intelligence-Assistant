from blackbox.graph.grounding import strip_unsupported, verify_claims

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
