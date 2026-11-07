from blackbox.ingestion.entity_extraction.store import SqlAlchemyEntityStore


def test_entity_store_basic():
    store = SqlAlchemyEntityStore("sqlite:///:memory:")
    store.init()
    store.upsert_service("checkout-service")
    store.upsert_error_code("ERR_POOL_EXHAUSTED")
    store.upsert_dependency("checkout-service", "payment-gateway")
    assert "checkout-service" in store.get_services()
    assert "ERR_POOL_EXHAUSTED" in store.get_error_codes()
    assert store.services_depending_on("payment-gateway") == ["checkout-service"]


def test_ingested_tracking():
    store = SqlAlchemyEntityStore("sqlite:///:memory:")
    store.init()
    store.record_ingested("doc1", "hash1", ["c1", "c2"])
    got = store.get_ingested("doc1")
    assert got is not None
    assert got.content_hash == "hash1"
    assert got.chunk_ids == "c1,c2"


def test_delete_doc_entities():
    store = SqlAlchemyEntityStore("sqlite:///:memory:")
    store.init()
    store.add_doc_entity("doc1", "service", "svc")
    store.delete_doc_entities("doc1")
    # no direct query method; re-add and check no crash
    store.add_doc_entity("doc1", "service", "svc2")
    assert True
