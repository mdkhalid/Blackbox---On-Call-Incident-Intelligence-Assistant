"""Groundedness and abstention evaluation for the corrective-RAG graph.

Two different questions are measured, and they are not interchangeable:

* **Claim support** — of the claims the generator drafted, what fraction traced to a
  retrieved chunk? This is measured from `ground_check` verdicts *before* unsupported
  claims are stripped, so it quantifies how often generation overreached. (The shipped
  answer is grounded by construction; reporting only that would be a tautology.)
* **Abstention** — on questions the corpus genuinely cannot answer, does the graph take
  the honest-failure path instead of inventing precedent?

The checker is the same deterministic `verify_claims` definition the graph uses. That is
stated in the output rather than hidden: the metric says "traces to retrieved evidence",
not "matches a hidden reference answer".

Run:
    python -m blackbox.eval.groundedness_eval
    python -m blackbox.eval.groundedness_eval --limit 10 --write
"""
from __future__ import annotations

import argparse
import json
import pathlib
from dataclasses import asdict, dataclass

from blackbox.config.settings import get_config, get_settings
from blackbox.eval.retrieval_eval import build_retriever, load_dataset
from blackbox.graph import build_graph, initial_state, run_graph
from blackbox.ingestion.entity_extraction.store import SqlAlchemyEntityStore
from blackbox.llm import build_llm_provider

RESULTS_DOC = pathlib.Path("docs/groundedness-results.md")

UNANSWERABLE_QUESTIONS = [
    "What is the capital of France?",
    "Which past incidents involved ERR_QUANTUM_FLUX in the telemetry pipeline?",
    "What did the 2021 Kubernetes upgrade retrospective say?",
    "Who signed off on the Mars datacenter migration in 1999?",
    "What is the SLA for the lunar telemetry pipeline?",
]


@dataclass
class GroundednessResult:
    question_id: str
    question: str
    category: str
    status: str
    route: str
    draft_claims: int = 0
    supported_claims: int = 0
    unsupported_claims: int = 0
    answer_chars: int = 0

    @property
    def support(self) -> float | None:
        if not self.draft_claims:
            return None
        return self.supported_claims / self.draft_claims


def run_question(graph, question: dict) -> GroundednessResult:
    out = run_graph(
        graph,
        initial_state(question["question"], filters=question.get("filter")),
    )
    ground = out.get("ground_check") or {}
    supported = int(ground.get("supported_count") or 0)
    unsupported = int(ground.get("unsupported_count") or 0)
    return GroundednessResult(
        question_id=str(question.get("id", "")),
        question=question["question"],
        category=str(question.get("category", "unknown")),
        status=str(out.get("status") or "error"),
        route=str(out.get("route") or "unknown"),
        draft_claims=supported + unsupported,
        supported_claims=supported,
        unsupported_claims=unsupported,
        answer_chars=len(out.get("answer") or ""),
    )


def summarize_results(results: list[GroundednessResult]) -> dict:
    answerable = [result for result in results if result.category != "unanswerable"]
    unanswerable = [result for result in results if result.category == "unanswerable"]
    answered = [result for result in results if result.status == "answered"]
    honest_failures = [result for result in results if result.status == "honest_failure"]
    direct = [result for result in results if result.status == "direct"]

    draft_claims = sum(result.draft_claims for result in results)
    supported = sum(result.supported_claims for result in results)
    unsupported = sum(result.unsupported_claims for result in results)

    abstained = [result for result in unanswerable if result.status == "honest_failure"]
    false_answers = [result for result in unanswerable if result.status == "answered"]
    answerable_answered = [result for result in answerable if result.status == "answered"]

    per_category: dict[str, dict] = {}
    for category in sorted({result.category for result in results}):
        items = [result for result in results if result.category == category]
        cat_claims = sum(item.draft_claims for item in items)
        cat_supported = sum(item.supported_claims for item in items)
        per_category[category] = {
            "n": len(items),
            "answered": sum(1 for item in items if item.status == "answered"),
            "honest_failures": sum(
                1 for item in items if item.status == "honest_failure"
            ),
            "claim_support": (
                round(cat_supported / cat_claims, 4) if cat_claims else None
            ),
        }

    return {
        "questions": len(results),
        "answerable": len(answerable),
        "unanswerable": len(unanswerable),
        "answered": len(answered),
        "honest_failures": len(honest_failures),
        "direct": len(direct),
        "answer_rate": round(len(answerable_answered) / len(answerable), 4)
        if answerable
        else None,
        "abstention_accuracy": round(len(abstained) / len(unanswerable), 4)
        if unanswerable
        else None,
        "false_answer_rate": round(len(false_answers) / len(unanswerable), 4)
        if unanswerable
        else None,
        "honest_failure_rate": round(len(honest_failures) / len(results), 4)
        if results
        else None,
        "draft_claims": draft_claims,
        "supported_claims": supported,
        "unsupported_claims": unsupported,
        "draft_claim_support": round(supported / draft_claims, 4)
        if draft_claims
        else None,
        "per_category": per_category,
    }


def evaluate_dataset(graph, dataset: dict, *, limit: int | None = None) -> dict:
    questions = list(dataset["questions"])
    if limit is not None:
        questions = questions[: max(0, limit)]
    questions += [
        {
            "id": f"u{index + 1:03d}",
            "question": question,
            "category": "unanswerable",
            "filter": None,
        }
        for index, question in enumerate(UNANSWERABLE_QUESTIONS)
    ]
    results = [run_question(graph, question) for question in questions]
    summary = summarize_results(results)
    summary["results"] = [asdict(result) | {"support": result.support} for result in results]
    return summary


def render_markdown(summary: dict, *, provider: str, embedder: str) -> str:
    lines = [
        "# Groundedness Results — Blackbox",
        "",
        "Generated by `python -m blackbox.eval.groundedness_eval`. Do not edit by hand.",
        "",
        "## Setup",
        "",
        (
            f"- Questions: {summary['answerable']} from `blackbox/eval/dataset.json` + "
            f"{summary['unanswerable']} deliberately unanswerable"
        ),
        f"- Graph LLM provider: `{provider}`",
        f"- Dense embeddings: `{embedder}`",
        (
            "- Claim checker: deterministic `verify_claims` over retrieved chunks (same as "
            "the graph's ground check). It measures traceability to evidence, not agreement "
            "with a reference answer."
        ),
        (
            "- Claim support is computed on the **drafted** claims before unsupported ones "
            "are stripped; the shipped answer is grounded by construction."
        ),
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Questions | {summary['questions']} |",
        f"| Answered | {summary['answered']} |",
        f"| Honest failures | {summary['honest_failures']} |",
        f"| Answer rate (answerable) | {_pct(summary['answer_rate'])} |",
        f"| Abstention accuracy (unanswerable) | {_pct(summary['abstention_accuracy'])} |",
        f"| False-answer rate (unanswerable) | {_pct(summary['false_answer_rate'])} |",
        f"| Drafted claims | {summary['draft_claims']} |",
        f"| Supported claims | {summary['supported_claims']} |",
        f"| Unsupported claims (stripped) | {summary['unsupported_claims']} |",
        f"| Draft claim support | {_pct(summary['draft_claim_support'])} |",
        "",
        "## Per-category",
        "",
        "| Category | Questions | Answered | Honest failures | Claim support |",
        "|---|---|---|---|---|",
    ]
    for category, values in summary["per_category"].items():
        lines.append(
            f"| {category} | {values['n']} | {values['answered']} | "
            f"{values['honest_failures']} | {_pct(values['claim_support'])} |"
        )

    lines += [
        "",
        "## Abstention detail",
        "",
        "| Question | Status | Claims (supported/total) |",
        "|---|---|---|",
    ]
    for result in summary["results"]:
        if result["category"] != "unanswerable":
            continue
        lines.append(
            f"| {result['question']} | {result['status']} | "
            f"{result['supported_claims']}/{result['draft_claims']} |"
        )

    lines += [
        "",
        "## Reading the numbers",
        "",
        (
            "- `draft_claim_support` is the honest groundedness number: it counts claims "
            "the generator drafted that did **not** trace to a retrieved chunk, before the "
            "ground check removed them."
        ),
        (
            "- When the offline deterministic provider is used, generation is extractive "
            "from the best-matching evidence sentence, so draft support is expected to be "
            "high. Re-run with a hosted model to measure the generator's real overreach; "
            "the harness and checker are unchanged."
        ),
        (
            "- Answer-rate failures concentrate in `paraphrase`: the seeded corpus is "
            "templated, so those questions carry no distinguishing signal for this retrieval "
            "setup (see `docs/retrieval-results.md`). They fail honestly instead of inventing "
            "a precedent."
        ),
        (
            "- `abstention_accuracy` is the no-answer path being measured, not merely "
            "demonstrated. A system that answers every question fails this row."
        ),
        "",
    ]
    return "\n".join(lines)


def _pct(value) -> str:
    return "—" if value is None else f"{value:.4f}"


def build_graph_for_eval(settings=None):
    settings = settings or get_settings()
    config = get_config()
    retriever = build_retriever()
    llm = build_llm_provider(settings)
    entity_store = SqlAlchemyEntityStore("sqlite:///data/entities.db")
    entity_store.init()
    graph = build_graph(
        retriever=retriever,
        llm=llm,
        entity_store=entity_store,
        settings=settings,
        config=config,
    )
    return graph, llm, retriever


def main() -> None:
    parser = argparse.ArgumentParser(description="Run groundedness + abstention evaluation")
    parser.add_argument("--limit", type=int, default=None, help="cap answerable questions")
    parser.add_argument("--write", action="store_true", help=f"write {RESULTS_DOC}")
    parser.add_argument("--json-out", default=None, help="dump raw results to this path")
    args = parser.parse_args()

    dataset = load_dataset()
    graph, llm, retriever = build_graph_for_eval()
    summary = evaluate_dataset(graph, dataset, limit=args.limit)

    print(f"questions: {summary['questions']}")
    print(f"answered: {summary['answered']} | honest failures: {summary['honest_failures']}")
    print(f"answer rate (answerable): {_pct(summary['answer_rate'])}")
    print(f"abstention accuracy: {_pct(summary['abstention_accuracy'])}")
    print(
        f"draft claim support: {_pct(summary['draft_claim_support'])} "
        f"({summary['supported_claims']}/{summary['draft_claims']})"
    )
    for category, values in summary["per_category"].items():
        print(f"  {category}: n={values['n']} support={_pct(values['claim_support'])}")

    provider = getattr(llm, "name", type(llm).__name__)
    embedder = retriever.vectorstore.embedding_provider.name
    if args.json_out:
        pathlib.Path(args.json_out).write_text(
            json.dumps(summary, indent=2) + "\n", encoding="utf-8"
        )
        print(f"wrote {args.json_out}")
    if args.write:
        RESULTS_DOC.write_text(
            render_markdown(summary, provider=provider, embedder=embedder),
            encoding="utf-8",
        )
        print(f"wrote {RESULTS_DOC}")


if __name__ == "__main__":
    main()
