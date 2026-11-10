import json

import pytest
from fastapi.testclient import TestClient

from blackbox.api.services import AppServices
from blackbox.api.store import SqlAlchemyConversationRepository
from blackbox.config.settings import AppConfig, MemoryConfig, RetrievalConfig
from blackbox.graph.build import build_graph
from blackbox.ingestion.entity_extraction.store import SqlAlchemyEntityStore
from blackbox.llm import FakeLLMProvider
from blackbox.main import create_app
from blackbox.retrieval.hybrid_retriever import build_hybrid_retriever


@pytest.fixture
def api_services(tmp_path, vectorstore, settings):
    retrieval = RetrievalConfig(
        top_k=3, candidate_pool=10, rerank=False, rerank_provider="none", rewrite_cap=2
    )
    config = AppConfig(
        retrieval=retrieval,
        memory=MemoryConfig(working_turns=2, summary_trigger_turns=3),
    )
    repo = SqlAlchemyConversationRepository(
        f"sqlite:///{tmp_path / 'conv.db'}", memory_config=config.memory
    )
    repo.init()
    entity_store = SqlAlchemyEntityStore(f"sqlite:///{tmp_path / 'entities.db'}")
    entity_store.init()
    retriever = build_hybrid_retriever(vectorstore, retrieval)
    llm = FakeLLMProvider()
    graph = build_graph(
        retriever=retriever,
        llm=llm,
        entity_store=entity_store,
        settings=settings,
        config=config,
    )
    return AppServices(
        settings=settings,
        config=config,
        conversations=repo,
        retriever=retriever,
        llm=llm,
        graph=graph,
        entity_store=entity_store,
        storage_ready=True,
        runtime_ready=True,
        storage_backend="sqlite",
    )


@pytest.fixture
def api_client(settings, api_services):
    return TestClient(create_app(settings, services=api_services))


def parse_sse(body: str) -> list[dict]:
    events: list[dict] = []
    for block in body.strip().split("\n\n"):
        if not block.strip():
            continue
        event = None
        data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
        events.append({"event": event, "data": data})
    return events


def chat(client, message: str, conversation_id: str | None = None) -> tuple[list[dict], dict]:
    payload = {"message": message}
    if conversation_id:
        payload["conversation_id"] = conversation_id
    response = client.post("/api/chat", json=payload)
    assert response.status_code == 200, response.text
    events = parse_sse(response.text)
    return events, events[-1]["data"]


def streamed_answer(events: list[dict]) -> str:
    return "".join(event["data"]["text"] for event in events if event["event"] == "token")


def test_chat_streams_nodes_tokens_and_persists(api_client):
    events, final = chat(api_client, "What caused ERR_POOL_EXHAUSTED in checkout-service?")

    kinds = [event["event"] for event in events]
    assert kinds[0] == "node"
    assert "token" in kinds
    assert kinds[-1] == "final"

    node_names = [event["data"]["node"] for event in events if event["event"] == "node"]
    assert node_names[0] == "router"
    assert "retrieve" in node_names
    assert "ground_check" in node_names

    assert final["status"] == "answered"
    assert final["answer"]
    assert streamed_answer(events) == final["answer"]

    conversation_id = final["conversation_id"]
    conversation = api_client.get(f"/api/conversations/{conversation_id}").json()
    assert conversation["turns"] == 1
    assert conversation["last_status"] == "answered"

    trace = api_client.get(f"/api/conversations/{conversation_id}/trace").json()
    assert len(trace["turns"]) == 1
    assert [e["node"] for e in trace["turns"][0]["trace"]][:2] == ["router", "retrieve"]
    assert trace["turns"][0]["chunks"]
    assert "fused_score" in trace["turns"][0]["chunks"][0]

    memory = api_client.get(f"/api/conversations/{conversation_id}/memory").json()
    assert "entity" in memory
    assert len(memory["working"]) == 1

    budget = api_client.get(f"/api/conversations/{conversation_id}/budget").json()
    assert budget["total_tokens"] <= budget["total_cap"]
    assert "retrieved_chunks" in budget["buckets"]


def test_chat_honest_failure_is_streamed_and_persisted(api_client):
    events, final = chat(api_client, "What is the capital of France?")

    assert final["status"] == "honest_failure"
    assert "didn't find" in final["answer"]
    assert "didn't find" in streamed_answer(events)

    conversation_id = final["conversation_id"]
    trace = api_client.get(f"/api/conversations/{conversation_id}/trace").json()
    nodes = [e["node"] for e in trace["turns"][0]["trace"]]
    assert "rewrite" in nodes
    assert nodes[-1] == "honest_failure"


def test_multi_turn_conversation_updates_memory_and_summary(api_client):
    _, first = chat(api_client, "We ruled out the 01:15 deploy: there was no code diff.")
    conversation_id = first["conversation_id"]

    chat(api_client, "Checkout p99 is spiking.", conversation_id)
    chat(api_client, "Pool utilisation is at 98 percent.", conversation_id)
    _, fourth = chat(api_client, "What caused this before?", conversation_id)

    trace = api_client.get(f"/api/conversations/{conversation_id}/trace").json()
    assert len(trace["turns"]) == 4

    memory = api_client.get(f"/api/conversations/{conversation_id}/memory").json()
    assert memory["summary"]["text"]
    assert "ruled out" in memory["summary"]["text"].lower()
    assert len(memory["working"]) <= 2
    assert fourth["status"] in ("answered", "honest_failure")


def test_unknown_conversation_returns_404(api_client):
    assert api_client.get("/api/conversations/missing").status_code == 404
    assert api_client.get("/api/conversations/missing/trace").status_code == 404
    assert api_client.get("/api/conversations/missing/memory").status_code == 404
    assert api_client.get("/api/conversations/missing/budget").status_code == 404


def test_blank_message_is_rejected(api_client):
    response = api_client.post("/api/chat", json={"message": "   "})
    assert response.status_code == 400


def test_oversized_message_is_rejected(api_client, api_services):
    limit = api_services.config.guardrails.max_message_chars
    response = api_client.post("/api/chat", json={"message": "x" * (limit + 1)})
    assert response.status_code == 413


def test_invalid_conversation_id_is_rejected(api_client):
    response = api_client.post(
        "/api/chat", json={"message": "hello", "conversation_id": "../../etc/passwd"}
    )
    assert response.status_code == 400
