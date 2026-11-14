"""Regression tests for the 2026-11-14 production audit fixes."""
from __future__ import annotations

import shutil
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from blackbox.api.services import AppServices
from blackbox.api.sse import public_update
from blackbox.api.store import SqlAlchemyConversationRepository
from blackbox.config.settings import AppConfig, get_settings
from blackbox.main import create_app

# ---- C2: ungrounded model fields never reach the wire ---------------------------


def test_public_update_strips_all_model_output_fields():
    values = {
        "answer": "fabricated sentence",
        "claims": [{"text": "fabricated sentence", "sources": []}],
        "verdicts": [{"claim": "fabricated sentence", "supported": False}],
        "trace": [{"node": "generate"}],
        "llm_calls": 3,
        "usage": {"total_tokens": 10},
    }
    out = public_update(values)
    assert "answer" not in out
    assert "claims" not in out
    assert "verdicts" not in out
    assert "trace" not in out
    assert out["llm_calls"] == 3


# ---- C1: repository binds the tenant for the RLS hook ---------------------------


def test_repository_binds_the_tenant(monkeypatch, tmp_path):
    import blackbox.api.store as store_module

    seen: list[str | None] = []

    @contextmanager
    def recorder(tenant):
        seen.append(tenant)
        yield

    monkeypatch.setattr(store_module, "set_current_tenant", recorder)
    repo = SqlAlchemyConversationRepository(f"sqlite:///{tmp_path / 'bind.db'}")
    repo.init()
    repo.get_or_create("c1", tenant_id="tenant-a")
    repo.get("c1", tenant_id="tenant-a")
    assert "tenant-a" in seen


# ---- C3: admin endpoints are tenant-scoped -------------------------------------


@pytest.fixture
def admin_services(tmp_path, monkeypatch):
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'admin.db'}")
    get_settings.cache_clear()
    settings = get_settings()
    repo = SqlAlchemyConversationRepository(f"sqlite:///{tmp_path / 'admin.db'}")
    repo.init()
    return AppServices(
        settings=settings,
        config=AppConfig(),
        conversations=repo,
        storage_ready=True,
        runtime_ready=True,
        storage_backend="sqlite",
    )


def test_offboarding_another_tenant_is_403(admin_services):
    admin_services.conversations.get_or_create("c1", tenant_id="tenant-b")
    client = TestClient(create_app(admin_services.settings, services=admin_services))

    response = client.delete("/api/admin/tenants/tenant-b")

    assert response.status_code == 403
    assert admin_services.conversations.get("c1", tenant_id="tenant-b") is not None


def test_retention_only_touches_the_principal_tenant(admin_services):
    repo = admin_services.conversations
    repo.get_or_create("mine", tenant_id="default")
    repo.get_or_create("theirs", tenant_id="tenant-b")
    repo.append_turn("mine", "q", "a", "answered", [], {}, tenant_id="default")
    repo.append_turn("theirs", "q", "a", "answered", [], {}, tenant_id="tenant-b")
    client = TestClient(create_app(admin_services.settings, services=admin_services))

    response = client.post("/api/admin/retention?days=0")

    assert response.status_code == 200
    assert repo.get_turns("mine", tenant_id="default") == []
    assert len(repo.get_turns("theirs", tenant_id="tenant-b")) == 1


# ---- month-end quota window ----------------------------------------------------


def test_quota_window_does_not_raise_on_month_end(monkeypatch):
    from blackbox.api import quotas

    class FakeDateTime:
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, 31, 12, 0, tzinfo=UTC)

    monkeypatch.setattr(quotas, "datetime", FakeDateTime)
    assert quotas._seconds_until_utc_midnight() > 0


# ---- memory compression preserves on an empty summary --------------------------


def test_empty_compression_keeps_summary_and_turns():
    from blackbox.config.settings import MemoryConfig
    from blackbox.llm.base import LLMResponse
    from blackbox.memory.conversation import ConversationMemory
    from blackbox.memory.models import Turn

    memory = ConversationMemory(
        config=MemoryConfig(working_turns=2, summary_trigger_turns=3)
    )
    memory.summary.text = "prior summary"
    memory.summary.turns_since_compression = 3
    memory.working.turns = [
        Turn(user=f"u{i}", assistant="a", timestamp=None) for i in range(4)
    ]

    class EmptyLLM:
        name = "empty"

        def complete(self, **kwargs):
            return LLMResponse(text="", model="fake")

    assert memory.maybe_compress(EmptyLLM()) is False
    assert memory.summary.text == "prior summary"
    assert len(memory.working.turns) == 4


# ---- C4: ingestion is tenant-scoped --------------------------------------------


class _Embedder:
    name = "test"
    dim = 8

    def embed(self, texts):
        return [[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0] for _ in texts]


def test_two_tenants_ingesting_the_same_doc_do_not_collide(tmp_path):
    from blackbox.ingestion.entity_extraction.store import SqlAlchemyEntityStore
    from blackbox.ingestion.pipeline import IngestionPipeline
    from blackbox.ingestion.vectorstore import ChromaVectorStore

    seed = tmp_path / "corpus" / "alerts"
    seed.mkdir(parents=True)
    src = (
        __import__("pathlib")
        .Path(__file__)
        .resolve()
        .parents[1]
        / "seed"
        / "corpus"
        / "alerts"
        / "alert-004.md"
    )
    shutil.copy(src, seed / "alert-004.md")

    vs = ChromaVectorStore(str(tmp_path / "chroma"), "tenant_chunks", _Embedder())
    es = SqlAlchemyEntityStore("sqlite:///:memory:")
    es.init()

    t1 = IngestionPipeline(vs, es, tenant_id="tenant-a")
    t2 = IngestionPipeline(vs, es, tenant_id="tenant-b")
    first = t1.ingest_document(seed / "alert-004.md", force=True)
    second = t2.ingest_document(seed / "alert-004.md", force=True)

    assert first["chunks"] > 0 and second["chunks"] > 0
    assert vs.count_where({"tenant_id": "tenant-a"}) == first["chunks"]
    assert vs.count_where({"tenant_id": "tenant-b"}) == second["chunks"]

    ids_a = vs.get_all(where={"tenant_id": "tenant-a"})["ids"]
    ids_b = vs.get_all(where={"tenant_id": "tenant-b"})["ids"]
    assert set(ids_a).isdisjoint(ids_b)

    # Deleting tenant B's document leaves tenant A's chunks intact.
    vs.delete_by_doc("alert-004", tenant_id="tenant-b")
    assert vs.count_where({"tenant_id": "tenant-a"}) == first["chunks"]
    assert vs.count_where({"tenant_id": "tenant-b"}) == 0
