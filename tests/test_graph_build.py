import threading
import time

import pytest

from blackbox.config.settings import AppConfig, GuardrailsConfig, RetrievalConfig
from blackbox.graph.build import build_graph, run_graph, stream_graph_events
from blackbox.graph.nodes import GraphDeps, retrieve_node
from blackbox.graph.state import initial_state
from blackbox.llm import FakeLLMProvider
from blackbox.llm.base import LLMResponse
from blackbox.retrieval.hybrid_retriever import build_hybrid_retriever
from blackbox.retrieval.models import RetrievalResult, RetrievedChunk


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


def test_retrieve_node_reports_this_request_timings(
    graph_retriever, graph_config, settings
):
    """The trace must carry the timings of the request it belongs to.

    The retriever is shared by every request, so timings read back from the instance
    would be whichever concurrent request published last.
    """
    deps = GraphDeps(
        retriever=graph_retriever,
        llm=FakeLLMProvider(),
        config=graph_config,
        settings=settings,
    )
    out = retrieve_node({"query": "checkout connection pool exhaustion"}, deps)
    assert "dense_ms" in out["timings"]
    assert "sparse_ms" in out["timings"]
    assert out["timings"]["last_retrieve_ms"] >= 0


def test_concurrent_retrieve_nodes_keep_their_own_timings(
    graph_retriever, graph_config, settings
):
    """Two in-flight requests must not see each other's retrieval latencies."""
    deps = GraphDeps(
        retriever=graph_retriever,
        llm=FakeLLMProvider(),
        config=graph_config,
        settings=settings,
    )
    slow_query = "ERR_POOL_EXHAUSTED checkout-service"
    fast_query = "pool saturation p99 latency"
    delays = {slow_query: 0.3, fast_query: 0.0}

    original_search = graph_retriever.dense.search

    def slow_dense_search(query, **kwargs):
        time.sleep(delays.get(query, 0.0))
        return original_search(query, **kwargs)

    graph_retriever.dense.search = slow_dense_search

    results: dict[str, dict] = {}

    def run(query: str) -> None:
        results[query] = retrieve_node({"query": query}, deps)

    threads = [threading.Thread(target=run, args=(query,)) for query in delays]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results[fast_query]["timings"]["dense_ms"] < results[slow_query]["timings"][
        "dense_ms"
    ]


def test_retrieve_node_takes_the_timings_of_its_own_call(graph_config, settings):
    """The node must read the timings off the result it just received.

    This is the deterministic form of the concurrency guard: a retriever that published
    its timings on the instance and let the node read them back would attribute
    whichever request published last to this one.
    """

    class StubRetriever:
        def retrieve_detailed(self, query, *, top_k, filter):
            return RetrievalResult(
                chunks=[
                    RetrievedChunk(
                        chunk_id=f"{query}::0",
                        text="evidence",
                        metadata={"doc_id": query},
                    )
                ],
                timings={"marker": query, "dense_ms": 1.0},
            )

    deps = GraphDeps(
        retriever=StubRetriever(),
        llm=FakeLLMProvider(),
        config=graph_config,
        settings=settings,
    )
    out = retrieve_node({"query": "checkout connection pool exhaustion"}, deps)
    assert out["timings"]["marker"] == "checkout connection pool exhaustion"
    assert out["timings"]["dense_ms"] == 1.0
    assert out["chunks"][0]["chunk_id"] == "checkout connection pool exhaustion::0"


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
