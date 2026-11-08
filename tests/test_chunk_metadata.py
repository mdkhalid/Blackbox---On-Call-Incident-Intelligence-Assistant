"""Chunk metadata must support filtered retrieval.

These assertions lock in the contract the retrieval layer depends on: lists stay lists
(for `$contains`), dates carry a numeric shadow field (for range filters), and values
Chroma rejects are never emitted.
"""
import pytest

from blackbox.ingestion.chunkers.base import make_metadata, to_epoch
from blackbox.ingestion.loaders import load_postmortem
from blackbox.ingestion.models import Document
from blackbox.retrieval.filters import MetadataFilter


def _doc(**meta) -> Document:
    base = {
        "services": ["checkout-service"],
        "severity": "SEV1",
        "date": "2024-05-08",
        "error_codes": ["ERR_POOL_EXHAUSTED"],
    }
    base.update(meta)
    return Document(
        doc_id="pm-001",
        doc_type="postmortem",
        title="t",
        metadata=base,
        sections={"Root Cause": "text"},
    )


def test_services_stay_a_list_for_contains_filtering():
    md = make_metadata(_doc(), "Root Cause")
    assert md["services"] == ["checkout-service"]
    assert isinstance(md["services"], list)


def test_error_codes_stay_a_list():
    md = make_metadata(_doc(), "Root Cause")
    assert md["error_codes"] == ["ERR_POOL_EXHAUSTED"]


def test_numeric_date_shadow_field_is_present():
    md = make_metadata(_doc(), "Root Cause")
    assert md["date"] == "2024-05-08"
    assert md["date_ord"] == to_epoch("2024-05-08")


def test_iso_strings_and_comma_strings_are_normalized():
    md = make_metadata(
        _doc(services="checkout-service, payment-gateway", error_codes="ERR_A, ERR_B"),
        "Root Cause",
    )
    assert md["services"] == ["checkout-service", "payment-gateway"]
    assert md["error_codes"] == ["ERR_A", "ERR_B"]


def test_empty_lists_are_omitted_chroma_rejects_them():
    md = make_metadata(_doc(services=[], error_codes=[]), "Root Cause")
    assert "services" not in md
    assert "error_codes" not in md


def test_missing_date_produces_no_date_fields():
    doc = _doc()
    del doc.metadata["date"]
    md = make_metadata(doc, "Root Cause")
    assert "date" not in md
    assert "date_ord" not in md


def test_invalid_date_keeps_string_but_skips_ordinal():
    md = make_metadata(_doc(date="unknown"), "Root Cause")
    assert md["date"] == "unknown"
    assert "date_ord" not in md


def test_metadata_is_accepted_by_the_filter_compiler():
    md = make_metadata(_doc(), "Root Cause")
    f = MetadataFilter(services=["checkout-service"], severities=["SEV1"])
    assert f.matches(md)


def test_real_corpus_chunk_metadata_works_with_filters(tmp_path):
    import sys

    sys.argv = ["x", "--out", str(tmp_path / "corpus")]
    from seed.generate_corpus import main as gen_main

    gen_main()
    pm_path = next((tmp_path / "corpus" / "postmortems").glob("*.md"))
    doc = load_postmortem(pm_path)
    from blackbox.ingestion.chunkers import chunk_postmortem

    chunks = list(chunk_postmortem(doc))
    assert chunks
    for chunk in chunks:
        md = chunk.metadata
        # Chroma rejects empty list metadata values.
        for key, value in md.items():
            if isinstance(value, list):
                assert value, f"empty list metadata for {key}"
        assert MetadataFilter(doc_types=["postmortem"]).matches(md)


def test_to_epoch_handles_invalid_input():
    assert to_epoch("not-a-date") is None
    assert to_epoch(None) is None
    assert to_epoch("2024-05-08") is not None


@pytest.mark.parametrize(
    "date,expected",
    [("2024-05-08", 739014), ("2023-11-02", 738826)],
)
def test_to_epoch_values(date, expected):
    assert to_epoch(date) == expected
