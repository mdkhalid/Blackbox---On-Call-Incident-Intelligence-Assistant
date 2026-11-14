"""Per-region retrieval topology, naming and rebuild path."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from blackbox.config.settings import AppConfig, RegionConfig, get_settings
from blackbox.retrieval import _region_scoped
from blackbox.retrieval.opensearch import OpenSearchSparseStore, index_mapping

ROOT = Path(__file__).resolve().parent.parent


def test_index_mapping_reflects_topology():
    mapping = index_mapping(shards=3, replicas=2)
    assert mapping["settings"]["index"]["number_of_shards"] == 3
    assert mapping["settings"]["index"]["number_of_replicas"] == 2


def test_sparse_store_uses_configured_topology():
    store = OpenSearchSparseStore(
        hosts="http://localhost:9200", index="chunks", replica_count=2, shard_count=3
    )
    assert store.replica_count == 2
    assert store.shard_count == 3
    assert store.mapping["settings"]["index"]["number_of_replicas"] == 2


def test_sparse_store_alias_mode():
    store = OpenSearchSparseStore(
        hosts="http://localhost:9200", index="blackbox_chunks", alias="chunks-current"
    )
    assert store.index == "chunks-current"
    assert store.alias == "chunks-current"


def test_region_scoped_name_off_by_default(monkeypatch):
    monkeypatch.setattr(
        "blackbox.retrieval.get_config",
        lambda: AppConfig(region=RegionConfig(region_suffix=False)),
    )
    settings = get_settings()
    assert _region_scoped("blackbox_chunks", settings) == "blackbox_chunks"


def test_region_scoped_name_appends_region(monkeypatch):
    monkeypatch.setattr(
        "blackbox.retrieval.get_config",
        lambda: AppConfig(region=RegionConfig(region_suffix=True)),
    )
    settings = get_settings()
    assert _region_scoped("blackbox_chunks", settings) == "blackbox_chunks_local"


@pytest.fixture
def mini_corpus_dir(tmp_path):
    root = tmp_path / "corpus"
    src = ROOT / "seed" / "corpus"
    (root / "postmortems").mkdir(parents=True)
    (root / "alerts").mkdir(parents=True)
    pm = next((src / "postmortems").glob("*.md"))
    alert = next((src / "alerts").glob("*.md"))
    shutil.copy(pm, root / "postmortems" / pm.name)
    shutil.copy(alert, root / "alerts" / alert.name)
    return root


def test_rebuild_region_indexes_validates(monkeypatch, tmp_path, mini_corpus_dir):
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("CHROMA_PERSIST_DIR", str(tmp_path / "chroma"))
    monkeypatch.setenv("EMBEDDING_PROVIDER", "fake")
    monkeypatch.setenv("REGION", "eu-west-1")
    monkeypatch.setenv("CONFIG_PATH", "blackbox/config/config.yaml")
    get_settings.cache_clear()

    from blackbox.ingestion.rebuild import rebuild_region_indexes

    settings = get_settings()
    result = rebuild_region_indexes(
        settings,
        AppConfig(),
        source_dir=mini_corpus_dir,
        tenant_id="default",
    )

    assert result["region"] == "eu-west-1"
    assert result["staged"] is False
    assert result["validation"]["passed"] is True
    assert result["validation"]["chunks_total"] > 0
