from pathlib import Path

REQUIRED = [
    "docs/architecture.png",
    "docs/chunking-strategy.md",
    "docs/memory-design.md",
    "docs/retrieval-results.md",
    "docs/groundedness-results.md",
    "docs/demo-script.md",
    "docs/scaling-and-production-readiness.md",
    "docs/development-tracker.md",
]


def test_required_submission_docs_exist():
    missing = [path for path in REQUIRED if not Path(path).is_file()]
    assert not missing, f"missing required docs: {missing}"


def test_architecture_diagram_is_a_png():
    signature = Path("docs/architecture.png").read_bytes()[:8]
    assert signature == b"\x89PNG\r\n\x1a\n"


def test_ci_gates_on_lint_types_and_tests():
    """A type-check step that is not wired into CI lets the error count grow."""
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "ruff check" in workflow
    assert "mypy blackbox" in workflow
    assert "pytest" in workflow
