import hashlib
import math
import re

import pytest
from fastapi.testclient import TestClient

from blackbox.config.settings import RetrievalConfig, get_settings
from blackbox.ingestion.models import Chunk
from blackbox.ingestion.vectorstore import ChromaVectorStore
from blackbox.main import create_app


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    monkeypatch.setenv("CONFIG_PATH", "blackbox/config/config.yaml")
    # Tests must not depend on a reachable Postgres from .env.
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    return get_settings()


@pytest.fixture
def client(settings):
    app = create_app(settings)
    return TestClient(app)


class HashingEmbeddingProvider:
    """Deterministic bag-of-words hashing embeddings.

    Tests need dense retrieval whose similarity actually tracks shared terms, so this
    hashes word tokens into buckets and L2-normalises. It is not semantic like a real
    model, but it is stable and offline, which is what unit tests need.
    """

    def __init__(self, dim: int = 64):
        self._dim = dim

    def _vec(self, text: str) -> list[float]:
        vec = [0.0] * self._dim
        for token in re.findall(r"[a-z0-9_]+", text.lower()):
            bucket = int(hashlib.md5(token.encode()).hexdigest(), 16) % self._dim
            vec[bucket] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    def embed(self, texts):
        return [self._vec(t) for t in texts]

    @property
    def dim(self):
        return self._dim

    @property
    def name(self):
        return "hashing"


@pytest.fixture
def embedder():
    return HashingEmbeddingProvider(dim=64)


@pytest.fixture
def retrieval_config():
    return RetrievalConfig(
        top_k=5,
        candidate_pool=10,
        fusion_method="rrf",
        fusion_k=60,
        dense_weight=0.5,
        sparse_weight=0.5,
        rerank=True,
        rerank_provider="lexical",
        rerank_candidates=10,
        retrieval_timeout_ms=5000,
    )


@pytest.fixture
def mini_corpus():
    """Small, internally consistent incident corpus for retrieval unit tests."""
    return [
        Chunk(
            chunk_id="pm-001::Root Cause::0",
            text=(
                "The root cause was ERR_POOL_EXHAUSTED in checkout-service. The database "
                "connection pool became saturated because upstream slowed down, so requests "
                "piled up and everything slowed down."
            ),
            metadata={
                "doc_id": "pm-001",
                "doc_type": "postmortem",
                "title": "checkout-service ERR_POOL_EXHAUSTED incident",
                "section": "Root Cause",
                "services": ["checkout-service", "payment-gateway"],
                "error_codes": ["ERR_POOL_EXHAUSTED"],
                "severity": "SEV1",
                "date": "2024-05-08",
                "date_ord": 739014,
                "part": 0,
            },
        ),
        Chunk(
            chunk_id="pm-002::Root Cause::0",
            text=(
                "Root cause: a bad TLS certificate caused ERR_AUTH_FAILED in auth-service. "
                "Tokens were rejected after certificate rotation."
            ),
            metadata={
                "doc_id": "pm-002",
                "doc_type": "postmortem",
                "title": "auth-service ERR_AUTH_FAILED incident",
                "section": "Root Cause",
                "services": ["auth-service"],
                "error_codes": ["ERR_AUTH_FAILED"],
                "severity": "SEV2",
                "date": "2023-11-02",
                "date_ord": 738826,
                "part": 0,
            },
        ),
        Chunk(
            chunk_id="rb-001::Symptom::0",
            text=(
                "checkout-service reports ERR_POOL_EXHAUSTED; p99 latency is spiking and "
                "requests pile up in the connection pool."
            ),
            metadata={
                "doc_id": "rb-001",
                "doc_type": "runbook",
                "title": "Runbook: checkout-service pool exhaustion",
                "section": "Symptom",
                "services": ["checkout-service"],
                "error_codes": ["ERR_POOL_EXHAUSTED"],
                "severity": "SEV1",
                "date": "2024-03-14",
                "date_ord": 738959,
                "part": 0,
            },
        ),
        Chunk(
            chunk_id="arch-001::Dependencies::0",
            text=(
                "The checkout-service depends on payment-gateway for authorisation and on "
                "the orders database. The payment gateway is a hard dependency."
            ),
            metadata={
                "doc_id": "arch-001",
                "doc_type": "arch",
                "title": "checkout-service architecture",
                "section": "Dependencies",
                "services": ["checkout-service", "payment-gateway"],
                "date": "2024-01-20",
                "date_ord": 738905,
                "part": 0,
            },
        ),
        Chunk(
            chunk_id="alert-001::Condition::0",
            text=(
                "Alert CheckoutPoolSaturation fires when the checkout-service connection "
                "pool utilisation exceeds 90 percent for five minutes."
            ),
            metadata={
                "doc_id": "alert-001",
                "doc_type": "alert",
                "title": "CheckoutPoolSaturation",
                "section": "Condition",
                "services": ["checkout-service"],
                "severity": "SEV1",
                "date": "2024-06-01",
                "date_ord": 739038,
                "part": 0,
            },
        ),
    ]


@pytest.fixture
def vectorstore(tmp_path, embedder, mini_corpus):
    vs = ChromaVectorStore(str(tmp_path / "chroma"), "test_chunks", embedder)
    vs.upsert_chunks(mini_corpus)
    return vs


@pytest.fixture
def entity_store():
    from blackbox.ingestion.entity_extraction.store import SqlAlchemyEntityStore

    store = SqlAlchemyEntityStore("sqlite:///:memory:")
    store.init()
    store.upsert_service("checkout-service")
    store.upsert_service("payment-gateway")
    store.upsert_service("auth-service")
    store.upsert_dependency("checkout-service", "payment-gateway")
    return store

