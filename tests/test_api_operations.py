import pytest
from fastapi.testclient import TestClient

from blackbox.api.services import AppServices
from blackbox.api.store import SqlAlchemyConversationRepository
from blackbox.config.settings import AppConfig, GuardrailsConfig, RetrievalConfig
from blackbox.eval.metrics import Metrics
from blackbox.graph.build import build_graph
from blackbox.ingestion.entity_extraction.store import SqlAlchemyEntityStore
from blackbox.llm import FakeLLMProvider
from blackbox.main import create_app
from blackbox.retrieval.hybrid_retriever import build_hybrid_retriever


@pytest.fixture
def operations_services(tmp_path, vectorstore, settings):
    retrieval = RetrievalConfig(
        top_k=3, candidate_pool=10, rerank=False, rerank_provider="none", rewrite_cap=2
    )
    config = AppConfig(
        retrieval=retrieval, guardrails=GuardrailsConfig(rate_limit_rps=100)
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
    )


@pytest.fixture
def operations_client(settings, operations_services):
    return TestClient(create_app(settings, services=operations_services))


def test_ingest_rejects_missing_corpus_dir(operations_client):
    response = operations_client.post("/api/ingest", json={"corpus_dir": "does/not/exist"})
    assert response.status_code == 400


def test_ingest_rejects_paths_outside_the_workspace(operations_client, tmp_path):
    response = operations_client.post(
        "/api/ingest", json={"corpus_dir": str(tmp_path / "outside")}
    )
    assert response.status_code == 400


def test_ingest_seeds_corpus_and_invalidates_retriever(operations_client, operations_services):
    before = operations_services.retriever.corpus_size
    response = operations_client.post(
        "/api/ingest", json={"corpus_dir": "seed/corpus"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["documents"] == 60
    assert body["chunks"] > before
    assert body["skipped"] == 0
    assert operations_services.retriever.corpus_size == body["chunks"]


def test_eval_run_returns_serialised_metrics(operations_client, monkeypatch):
    def fake_run_all(retriever, dataset, *, k=5, strategies=None):
        return {
            "hybrid (RRF)": (
                Metrics(count=1, mrr=0.5, recall=0.5, precision=0.5, hit_rate=1.0),
                [],
            )
        }

    monkeypatch.setattr("blackbox.api.routes.operations.run_all", fake_run_all)
    response = operations_client.post("/api/eval/run", json={"k": 5})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["k"] == 5
    assert body["strategies"]["hybrid (RRF)"]["mrr"] == 0.5


def test_eval_run_includes_groundedness_when_requested(operations_client, monkeypatch):
    def fake_run_all(retriever, dataset, *, k=5, strategies=None):
        return {"hybrid (RRF)": (Metrics(count=1, mrr=0.5), [])}

    async def fake_groundedness(graph, dataset, *, limit=None):
        return {"draft_claim_support": 0.75, "abstention_accuracy": 1.0}

    monkeypatch.setattr("blackbox.api.routes.operations.run_all", fake_run_all)
    monkeypatch.setattr(
        "blackbox.api.routes.operations.evaluate_dataset", fake_groundedness
    )
    response = operations_client.post(
        "/api/eval/run", json={"k": 5, "groundedness": True}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["groundedness"]["draft_claim_support"] == 0.75
    assert body["groundedness"]["abstention_accuracy"] == 1.0


def test_eval_run_rejects_a_concurrent_run(operations_client, operations_services):
    assert operations_services.eval_lock.acquire(blocking=False)
    try:
        response = operations_client.post("/api/eval/run", json={"k": 5})
        assert response.status_code == 409
    finally:
        operations_services.eval_lock.release()
