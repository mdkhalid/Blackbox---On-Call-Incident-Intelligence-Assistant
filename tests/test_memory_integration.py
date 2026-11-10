import pytest

from blackbox.config.settings import AppConfig, BudgetConfig, MemoryConfig, RetrievalConfig
from blackbox.graph.build import build_graph, run_graph
from blackbox.graph.state import initial_state
from blackbox.llm import FakeLLMProvider
from blackbox.memory import ConversationMemory, build_context
from blackbox.retrieval.hybrid_retriever import build_hybrid_retriever


@pytest.fixture
def graph_retriever(vectorstore):
    config = RetrievalConfig(
        top_k=3, candidate_pool=10, rerank=False, rerank_provider="none", rewrite_cap=2
    )
    return build_hybrid_retriever(vectorstore, config)


@pytest.fixture
def graph_config():
    return AppConfig(
        retrieval=RetrievalConfig(
            top_k=3, candidate_pool=10, rerank=False, rerank_provider="none", rewrite_cap=2
        )
    )


def _memory() -> ConversationMemory:
    memory = ConversationMemory(MemoryConfig(working_turns=2, summary_trigger_turns=3))
    memory.entity.incident_id = "INC-2024-0315"
    memory.entity.note_services(["checkout-service"])
    memory.entity.upsert_hypothesis(
        "DB pool exhaustion", status="confirmed", evidence="pm-001 root cause"
    )
    memory.entity.upsert_hypothesis(
        "01:15 deploy regression", status="ruled_out", evidence="no code diff"
    )
    memory.add_turn(
        "We ruled out the 01:15 deploy because there was no code diff.",
        "Noted, deploy is ruled out.",
    )
    memory.add_turn("Checkout p99 is still spiking.", "Pool metrics look saturated.")
    memory.add_turn("What should we check next?", "Connection pool utilisation.")
    memory.maybe_compress(FakeLLMProvider())
    return memory


def test_generate_receives_the_memory_context(graph_retriever, graph_config, settings):
    llm = FakeLLMProvider()
    memory = _memory()
    chunks = [
        {
            "chunk_id": "c1",
            "doc_id": "pm-001",
            "section": "Root Cause",
            "text": "The root cause was ERR_POOL_EXHAUSTED in checkout-service.",
        }
    ]
    bundle = build_context(
        system_prompt="You are Blackbox.",
        memory=memory,
        chunks=chunks,
        question="What caused the outage?",
        budget=BudgetConfig(),
    )

    graph = build_graph(
        retriever=graph_retriever, llm=llm, config=graph_config, settings=settings
    )
    out = run_graph(
        graph,
        initial_state(
            "What caused ERR_POOL_EXHAUSTED in checkout-service?",
            context=bundle.render(),
            budget=bundle.report,
        ),
    )

    generate_prompts = [call["prompt"] for call in llm.calls if call["task"] == "generate"]
    assert generate_prompts
    prompt = generate_prompts[-1]
    assert "DB pool exhaustion" in prompt
    assert "INC-2024-0315" in prompt
    assert "INCIDENT SUMMARY" in prompt
    assert out["status"] == "answered"
    assert out["budget"]["total_tokens"] <= out["budget"]["total_cap"]


def test_long_conversation_stays_within_budget_and_keeps_ruled_out_fact():
    memory = ConversationMemory(MemoryConfig(working_turns=6, summary_trigger_turns=8))
    llm = FakeLLMProvider()
    for i in range(40):
        memory.add_turn(
            f"Turn {i}: we ruled out hypothesis {i} after checking metrics.",
            f"Assistant recorded turn {i}.",
        )
        memory.maybe_compress(llm)

    assert len(memory.working.recent()) <= 6
    assert "ruled out" in memory.summary.text.lower()

    bundle = build_context(
        system_prompt="system",
        memory=memory,
        chunks=[],
        question="Where are we?",
        budget=BudgetConfig(),
    )
    assert bundle.report["total_tokens"] <= bundle.report["total_cap"]
    assert bundle.report["buckets"]["summary"]["allocated"] <= BudgetConfig().summary_tokens
