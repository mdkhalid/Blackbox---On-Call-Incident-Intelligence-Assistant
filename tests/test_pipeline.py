
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
