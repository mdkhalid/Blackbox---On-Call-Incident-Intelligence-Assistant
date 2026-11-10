import json

import pytest

from blackbox.config.settings import AppConfig, RetrievalConfig
from blackbox.eval.groundedness_eval import (
    GroundednessResult,
    run_question,
    summarize_results,
)
from blackbox.graph.build import build_graph
from blackbox.llm import FakeLLMProvider
from blackbox.llm.base import LLMResponse
from blackbox.retrieval.hybrid_retriever import build_hybrid_retriever


class HallucinatingLLM(FakeLLMProvider):
    """Drafts one grounded claim and one invented claim, to exercise the checker."""

    def complete(self, **kwargs):
        if "TASK: GENERATE" in kwargs.get("prompt", ""):
            prompt = kwargs["prompt"]
            chunk_id = prompt.split("[", 1)[1].split("]", 1)[0]
            return LLMResponse(
                text=json.dumps(
                    {
                        "answer": (
                            "The connection pool was saturated ["
                            + chunk_id
                            + "]. The moon is made of cheese ["
                            + chunk_id
                            + "]."
                        ),
                        "claims": [
                            {
                                "text": "The connection pool was saturated",
                                "sources": [chunk_id],
                            },
                            {
                                "text": "The moon is made of cheese",
                                "sources": [chunk_id],
                            },
                        ],
                    }
                ),
                model="scripted",
                prompt_tokens=10,
                completion_tokens=10,
            )
        return super().complete(**kwargs)


@pytest.fixture
def eval_graph(vectorstore, settings):
    retrieval = RetrievalConfig(
        top_k=3, candidate_pool=10, rerank=False, rerank_provider="none", rewrite_cap=2
    )
    config = AppConfig(retrieval=retrieval)
    retriever = build_hybrid_retriever(vectorstore, retrieval)
    return build_graph(
        retriever=retriever, llm=HallucinatingLLM(), settings=settings, config=config
    )


def test_run_question_counts_unsupported_draft_claims(eval_graph):
    result = run_question(
        eval_graph,
        {
            "id": "q1",
            "question": "What caused the connection pool saturation?",
            "category": "structural",
        },
    )
    assert result.status == "answered"
    assert result.draft_claims == 2
    assert result.supported_claims == 1
    assert result.unsupported_claims == 1
    assert result.support == 0.5


def test_summary_computes_support_answer_rate_and_abstention():
    results = [
        GroundednessResult("q1", "a", "structural", "answered", "retrieval", 2, 1, 1, 50),
        GroundednessResult("q2", "b", "structural", "honest_failure", "retrieval"),
        GroundednessResult("u1", "c", "unanswerable", "honest_failure", "retrieval"),
    ]
    summary = summarize_results(results)

    assert summary["questions"] == 3
    assert summary["answer_rate"] == 0.5
    assert summary["abstention_accuracy"] == 1.0
    assert summary["false_answer_rate"] == 0.0
    assert summary["draft_claims"] == 2
    assert summary["supported_claims"] == 1
    assert summary["draft_claim_support"] == 0.5


def test_summary_flags_false_answers_on_unanswerable_questions():
    results = [
        GroundednessResult("u1", "c", "unanswerable", "answered", "retrieval", 1, 1, 0, 20),
        GroundednessResult("u2", "d", "unanswerable", "honest_failure", "retrieval"),
    ]
    summary = summarize_results(results)
    assert summary["abstention_accuracy"] == 0.5
    assert summary["false_answer_rate"] == 0.5


def test_summary_handles_no_claims():
    results = [
        GroundednessResult("q1", "a", "structural", "honest_failure", "retrieval"),
    ]
    summary = summarize_results(results)
    assert summary["draft_claim_support"] is None
    assert summary["answer_rate"] == 0.0
