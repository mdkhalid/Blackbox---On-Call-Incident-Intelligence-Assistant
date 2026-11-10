import pytest

from blackbox.config.settings import AppConfig, GuardrailsConfig, RetrievalConfig
from blackbox.graph.build import build_graph, run_graph, stream_graph_events
from blackbox.graph.state import initial_state
from blackbox.llm import FakeLLMProvider
from blackbox.llm.base import LLMResponse
from blackbox.retrieval.hybrid_retriever import build_hybrid_retriever


@pytest.fixture
def graph_retriever(vectorstore):
    cfg = RetrievalConfig(
        top_k=3,
        candidate_pool=10,
        rerank=False,
        rerank_provider="none",
        rewrite_cap=2,
    )
    return build_hybrid_retriever(vectorstore, cfg)


@pytest.fixture
def graph_config():
    return AppConfig(
        retrieval=RetrievalConfig(
            top_k=3,
            candidate_pool=10,
            rerank=False,
            rerank_provider="none",
            rewrite_cap=2,
        )
    )


class AlwaysInsufficient(FakeLLMProvider):
    """Grades every retrieval as insufficient, so the bounded rewrite loop is exercised."""

    def complete(self, **kwargs):
        if "TASK: GRADE" in kwargs.get("prompt", ""):
            resp = LLMResponse(
                text='{"sufficient": false, "reason": "evidence does not answer the question"}',
                model="fake",
                prompt_tokens=10,
                completion_tokens=5,
            )
            self._record(kwargs.get("prompt", ""), resp)
            return resp
        return super().complete(**kwargs)

    def _record(self, prompt, resp):
        self.calls.append({"prompt": prompt, "response": resp.text})
        self.usage["prompt_tokens"] += resp.prompt_tokens
        self.usage["completion_tokens"] += resp.completion_tokens
        self.usage["total_tokens"] += resp.prompt_tokens + resp.completion_tokens


def test_graph_answers_grounded_question(graph_retriever, graph_config, settings):
    graph = build_graph(
        retriever=graph_retriever, llm=FakeLLMProvider(), config=graph_config, settings=settings
    )
    out = run_graph(
        graph, initial_state("What caused ERR_POOL_EXHAUSTED in checkout-service?")
    )
    assert out["status"] == "answered"
    assert out["route"] == "retrieval"
    assert out["answer"]
    assert out["ground_check"]["supported_count"] >= 1
    nodes = [event["node"] for event in out["trace"]]
    assert nodes[0] == "router"
    for node in ("retrieve", "grade", "generate", "ground_check"):
        assert node in nodes


def test_trace_includes_the_retrieval_score_trail(graph_retriever, graph_config, settings):
    graph = build_graph(
        retriever=graph_retriever, llm=FakeLLMProvider(), config=graph_config, settings=settings
    )
    out = run_graph(graph, initial_state("checkout connection pool exhaustion"))
    assert out["chunks"]
    assert any(chunk.get("fused_rank") is not None for chunk in out["chunks"])


def test_graph_honest_failure_after_rewrite_cap(graph_retriever, graph_config, settings):
    graph = build_graph(
        retriever=graph_retriever,
        llm=AlwaysInsufficient(),
        config=graph_config,
        settings=settings,
    )
    out = run_graph(
        graph,
        initial_state(
            "What caused ERR_POOL_EXHAUSTED?", filters={"doc_types": ["postmortem"]}
        ),
    )
    assert out["status"] == "honest_failure"
    assert out["rewrite_count"] == graph_config.retrieval.rewrite_cap
    assert len(out["grade_history"]) == graph_config.retrieval.rewrite_cap + 1
    assert "ERR_POOL_EXHAUSTED" in out["answer"]
    assert "postmortem" in out["answer"].lower()
    nodes = [event["node"] for event in out["trace"]]
    assert nodes.count("rewrite") == graph_config.retrieval.rewrite_cap


def test_graph_enforces_the_llm_call_cap(graph_retriever, settings):
    config = AppConfig(
        retrieval=RetrievalConfig(
            top_k=3,
            candidate_pool=10,
            rerank=False,
            rerank_provider="none",
            rewrite_cap=2,
        ),
        guardrails=GuardrailsConfig(max_llm_calls_per_turn=1),
    )
    graph = build_graph(
        retriever=graph_retriever, llm=AlwaysInsufficient(), config=config, settings=settings
    )
    out = run_graph(graph, initial_state("What caused ERR_POOL_EXHAUSTED?"))
    assert out["status"] == "honest_failure"
    assert out["error"] == "llm_call_cap"
    assert out["llm_calls"] == 1


def test_graph_entity_lookup_route(graph_retriever, graph_config, settings, entity_store):
    graph = build_graph(
        retriever=graph_retriever,
        llm=FakeLLMProvider(),
        entity_store=entity_store,
        config=graph_config,
        settings=settings,
    )
    out = run_graph(graph, initial_state("Which services depend on payment-gateway?"))
    assert out["route"] == "entity"
    assert out["status"] == "answered"
    assert "checkout-service" in out["answer"]


def test_graph_direct_route(graph_retriever, graph_config, settings):
    graph = build_graph(
        retriever=graph_retriever, llm=FakeLLMProvider(), config=graph_config, settings=settings
    )
    out = run_graph(graph, initial_state("hello"))
    assert out["status"] == "direct"
    assert "incident" in out["answer"].lower()


def test_streaming_emits_node_events_and_a_final_state(
    graph_retriever, graph_config, settings
):
    graph = build_graph(
        retriever=graph_retriever, llm=FakeLLMProvider(), config=graph_config, settings=settings
    )

    async def collect():
        return [
            event
            async for event in stream_graph_events(
                graph, initial_state("What caused ERR_POOL_EXHAUSTED?")
            )
        ]

    import asyncio

    events = asyncio.run(collect())
    node_events = [e for e in events if e["event"] == "node"]
    assert node_events[0]["node"] == "router"
    assert events[-1]["event"] == "final"
    assert events[-1]["data"]["status"] == "answered"
