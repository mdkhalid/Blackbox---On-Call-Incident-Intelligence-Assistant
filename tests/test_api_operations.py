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
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]

    body = _await_job(operations_client, f"/api/ingest/{job_id}")
    print("JOB ERROR:", body.get("error"))
    assert body["status"] == "succeeded", body
    assert body["result"]["documents"] == 60
    assert body["result"]["chunks"] > before
    assert body["result"]["skipped"] == 0
    assert operations_services.retriever.corpus_size == body["result"]["chunks"]


def _await_job(client, url: str, *, timeout_s: float = 60.0) -> dict:
    """Poll a job until it finishes."""
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        body = client.get(url).json()
        if body["status"] in ("succeeded", "failed"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"job did not finish: {url}")


def _cheap_run_all(retriever, dataset, *, k=5, strategies=None):
    """A stand-in for the real evaluation grid, which takes far too long for a test."""
    return {
        "hybrid (RRF)": (
            Metrics(count=1, mrr=0.5, recall=0.5, precision=0.5, hit_rate=1.0),
            [],
        )
    }


def _wait_for_job(services, job_id: str, *, timeout_s: float = 60.0) -> dict:
    """Wait for a job to finish, without going through the tenant-scoped endpoint."""
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        job = services.job_queue("eval").get(job_id)
        if job is not None and job.done:
            return job.to_dict()
        time.sleep(0.05)
    raise AssertionError(f"job did not finish: {job_id}")


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
    assert response.status_code == 202, response.text
    body = _await_job(operations_client, f"/api/eval/{response.json()['job_id']}")
    assert body["status"] == "succeeded", body
    assert body["result"]["k"] == 5
    assert body["result"]["strategies"]["hybrid (RRF)"]["mrr"] == 0.5


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
    assert response.status_code == 202, response.text
    body = _await_job(operations_client, f"/api/eval/{response.json()['job_id']}")
    assert body["status"] == "succeeded", body
    assert body["result"]["groundedness"]["draft_claim_support"] == 0.75
    assert body["result"]["groundedness"]["abstention_accuracy"] == 1.0


def test_a_second_eval_is_queued_rather_than_refused(operations_client, monkeypatch):
    """A long job must not make the API refuse the work while it runs."""
    monkeypatch.setattr(
        "blackbox.api.routes.operations.run_all", _cheap_run_all
    )
    first = operations_client.post("/api/eval/run", json={"k": 5})
    second = operations_client.post("/api/eval/run", json={"k": 5})
    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["job_id"] != second.json()["job_id"]

    for response in (first, second):
        body = _await_job(operations_client, f"/api/eval/{response.json()['job_id']}")
        assert body["status"] == "succeeded", body


def test_a_job_of_another_tenant_is_not_found(operations_client, operations_services, monkeypatch):
    monkeypatch.setattr(
        "blackbox.api.routes.operations.run_all", _cheap_run_all
    )
    response = operations_client.post("/api/eval/run", json={"k": 5})
    job_id = response.json()["job_id"]
    operations_services.job_queue("eval").get(job_id).tenant_id = "someone-else"
    assert operations_client.get(f"/api/jobs/{job_id}").status_code == 404
    assert operations_client.get(f"/api/eval/{job_id}").status_code == 404
    # The job itself still runs; it is only hidden from the other tenant.
    _wait_for_job(operations_services, job_id)


def test_an_unknown_job_is_not_found(operations_client):
    assert operations_client.get("/api/jobs/does-not-exist").status_code == 404
