
from seed.generate_corpus import main as gen_main


def test_corpus_counts(tmp_path):
    out = tmp_path / "corpus"
    import sys

    sys.argv = ["x", "--out", str(out)]
    gen_main()
    files = list(out.rglob("*.md"))
    assert len(files) == 60
    assert len(list((out / "postmortems").glob("*.md"))) == 30
    assert len(list((out / "runbooks").glob("*.md"))) == 15
    assert len(list((out / "arch").glob("*.md"))) == 10
    assert len(list((out / "alerts").glob("*.md"))) == 5


def test_corpus_deterministic(tmp_path):
    out1 = tmp_path / "c1"
    out2 = tmp_path / "c2"
    import sys

    sys.argv = ["x", "--out", str(out1)]
    gen_main()
    sys.argv = ["x", "--out", str(out2)]
    gen_main()
    f1 = sorted(p.read_text() for p in out1.rglob("*.md"))
    f2 = sorted(p.read_text() for p in out2.rglob("*.md"))
    assert f1 == f2


def test_postmortem_sections(tmp_path):
    out = tmp_path / "corpus"
    import sys

    sys.argv = ["x", "--out", str(out)]
    gen_main()
    pm = next((out / "postmortems").glob("*.md"))
    text = pm.read_text()
    assert "## Root Cause" in text
    assert "## Timeline" in text
    assert text.startswith("---")
