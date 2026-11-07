
from blackbox.ingestion.chunkers.postmortem import chunk as chunk_pm
from blackbox.ingestion.loaders import load_postmortem


def _corpus(tmp_path):
    import sys

    sys.argv = ["x", "--out", str(tmp_path / "corpus")]
    from seed.generate_corpus import main as gen_main

    gen_main()


def test_postmortem_root_cause_atomic(tmp_path):
    _corpus(tmp_path)
    pm = next((tmp_path / "corpus" / "postmortems").glob("*.md"))
    doc = load_postmortem(pm)
    chunks = list(chunk_pm(doc))
    rc_chunks = [c for c in chunks if c.metadata["section"] == "Root Cause"]
    # root cause should be one chunk (atomic) for normal-sized corpus
    assert len(rc_chunks) == 1
    assert "root cause" in rc_chunks[0].text.lower() or "Root Cause" in rc_chunks[0].text


def test_chunks_have_metadata(tmp_path):
    _corpus(tmp_path)
    pm = next((tmp_path / "corpus" / "postmortems").glob("*.md"))
    doc = load_postmortem(pm)
    chunks = list(chunk_pm(doc))
    assert len(chunks) >= 3
    for c in chunks:
        assert c.chunk_id
        assert c.metadata["doc_id"] == doc.doc_id
        assert c.metadata["doc_type"] == "postmortem"
        assert "section" in c.metadata
        assert "services" in c.metadata


def test_chunk_ids_deterministic(tmp_path):
    _corpus(tmp_path)
    pm = next((tmp_path / "corpus" / "postmortems").glob("*.md"))
    doc = load_postmortem(pm)
    a = [c.chunk_id for c in chunk_pm(doc)]
    b = [c.chunk_id for c in chunk_pm(doc)]
    assert a == b
