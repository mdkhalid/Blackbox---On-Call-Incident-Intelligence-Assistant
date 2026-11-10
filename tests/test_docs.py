from pathlib import Path

REQUIRED = [
    "docs/architecture.png",
    "docs/chunking-strategy.md",
    "docs/memory-design.md",
    "docs/retrieval-results.md",
    "docs/groundedness-results.md",
    "docs/demo-script.md",
]


def test_required_submission_docs_exist():
    missing = [path for path in REQUIRED if not Path(path).is_file()]
    assert not missing, f"missing required docs: {missing}"


def test_architecture_diagram_is_a_png():
    signature = Path("docs/architecture.png").read_bytes()[:8]
    assert signature == b"\x89PNG\r\n\x1a\n"
