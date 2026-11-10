
from blackbox.ingestion.embeddings import FakeEmbeddingProvider
from blackbox.ingestion.entity_extraction.store import SqlAlchemyEntityStore
from blackbox.ingestion.pipeline import IngestionPipeline
from blackbox.ingestion.vectorstore import ChromaVectorStore


def _corpus(tmp_path):
    import sys

    sys.argv = ["x", "--out", str(tmp_path / "corpus")]
    from seed.generate_corpus import main as gen_main

    gen_main()


def test_pipeline_ingests_corpus(tmp_path):
    _corpus(tmp_path)
    root = tmp_path / "corpus"
    emb = FakeEmbeddingProvider(dim=4)
    vs = ChromaVectorStore(str(tmp_path / "chroma"), "coll", emb)
    es = SqlAlchemyEntityStore(f"sqlite:///{tmp_path / 'e.db'}")
    es.init()
    pipe = IngestionPipeline(vs, es, batch_size=10)
    results = pipe.ingest_directory(root)
    assert len(results) == 60
    assert vs.count() > 0
    # services extracted
    assert len(es.get_services()) > 0


def test_pipeline_idempotent(tmp_path):
    _corpus(tmp_path)
    root = tmp_path / "corpus"
    emb = FakeEmbeddingProvider(dim=4)
    vs = ChromaVectorStore(str(tmp_path / "chroma"), "coll", emb)
    es = SqlAlchemyEntityStore(f"sqlite:///{tmp_path / 'e.db'}")
    es.init()
    pipe = IngestionPipeline(vs, es, batch_size=10)
    pipe.ingest_directory(root)
    count1 = vs.count()
    r2 = pipe.ingest_directory(root)
    count2 = vs.count()
    assert count1 == count2
    assert all(r["status"] == "skipped" for r in r2)


def test_pipeline_force_reingests_even_when_unchanged(tmp_path):
    _corpus(tmp_path)
    root = tmp_path / "corpus"
    vs = ChromaVectorStore(str(tmp_path / "chroma"), "coll", FakeEmbeddingProvider(dim=4))
    es = SqlAlchemyEntityStore(f"sqlite:///{tmp_path / 'e.db'}")
    es.init()
    pipe = IngestionPipeline(vs, es, batch_size=10)
    pipe.ingest_directory(root)
    count_before = vs.count()

    results = pipe.ingest_directory(root, force=True)

    assert all(result["status"] == "ingested" for result in results)
    assert vs.count() == count_before


def test_pipeline_reingests_when_vector_store_was_rebuilt(tmp_path):
    """A surviving entity DB must not suppress ingestion into an empty vector store.

    This is the Chroma-corruption recovery path: the store is recreated, but the ingest
    ledger (content hashes) survives. Skipping on the hash alone leaves retrieval empty.
    """
    _corpus(tmp_path)
    root = tmp_path / "corpus"
    es = SqlAlchemyEntityStore(f"sqlite:///{tmp_path / 'e.db'}")
    es.init()

    vs1 = ChromaVectorStore(str(tmp_path / "chroma1"), "coll", FakeEmbeddingProvider(dim=4))
    IngestionPipeline(vs1, es, batch_size=10).ingest_directory(root)

    vs2 = ChromaVectorStore(str(tmp_path / "chroma2"), "coll", FakeEmbeddingProvider(dim=4))
    results = IngestionPipeline(vs2, es, batch_size=10).ingest_directory(root)

    assert vs2.count() == vs1.count()
    assert all(result["status"] == "ingested" for result in results)
