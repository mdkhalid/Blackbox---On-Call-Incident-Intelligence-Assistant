
from blackbox.ingestion.loaders import load_alert, load_arch, load_postmortem, load_runbook


def _make_corpus(tmp_path):
    import sys

    sys.argv = ["x", "--out", str(tmp_path / "corpus")]
    from seed.generate_corpus import main as gen_main

    gen_main()


def test_load_postmortem(tmp_path):
    _make_corpus(tmp_path)
    pm = next((tmp_path / "corpus" / "postmortems").glob("*.md"))
    doc = load_postmortem(pm)
    assert doc.doc_type == "postmortem"
    assert "Root Cause" in doc.sections
    assert "Timeline" in doc.sections
    assert "services" in doc.metadata


def test_load_runbook(tmp_path):
    _make_corpus(tmp_path)
    rb = next((tmp_path / "corpus" / "runbooks").glob("*.md"))
    doc = load_runbook(rb)
    assert doc.doc_type == "runbook"
    assert "Symptom" in doc.sections or "Diagnosis" in doc.sections


def test_load_arch(tmp_path):
    _make_corpus(tmp_path)
    a = next((tmp_path / "corpus" / "arch").glob("*.md"))
    doc = load_arch(a)
    assert doc.doc_type == "arch"
    assert "Dependencies" in doc.sections


def test_load_alert(tmp_path):
    _make_corpus(tmp_path)
    a = next((tmp_path / "corpus" / "alerts").glob("*.md"))
    doc = load_alert(a)
    assert doc.doc_type == "alert"
    assert len(doc.sections) >= 1
