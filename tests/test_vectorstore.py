
from blackbox.ingestion.embeddings import FakeEmbeddingProvider
from blackbox.ingestion.models import Chunk
from blackbox.ingestion.vectorstore import ChromaVectorStore


def test_vectorstore_upsert_and_count(tmp_path):
    emb = FakeEmbeddingProvider(dim=4)
    vs = ChromaVectorStore(str(tmp_path / "chroma"), "test_coll", emb)
    chunks = [
        Chunk(chunk_id="c1", text="hello world", metadata={"doc_id": "d1", "section": "S"}),
        Chunk(chunk_id="c2", text="foo bar", metadata={"doc_id": "d1", "section": "S"}),
    ]
    n = vs.upsert_chunks(chunks)
    assert n == 2
    assert vs.count() == 2


def test_vectorstore_idempotent_upsert(tmp_path):
    emb = FakeEmbeddingProvider(dim=4)
    vs = ChromaVectorStore(str(tmp_path / "chroma"), "test_coll", emb)
    chunks = [Chunk(chunk_id="c1", text="hello", metadata={"doc_id": "d1", "section": "S"})]
    vs.upsert_chunks(chunks)
    vs.upsert_chunks(chunks)
    assert vs.count() == 1


def test_vectorstore_delete_by_doc(tmp_path):
    emb = FakeEmbeddingProvider(dim=4)
    vs = ChromaVectorStore(str(tmp_path / "chroma"), "test_coll", emb)
    chunks = [
        Chunk(chunk_id="c1", text="hello", metadata={"doc_id": "d1", "section": "S"}),
        Chunk(chunk_id="c2", text="world", metadata={"doc_id": "d2", "section": "S"}),
    ]
    vs.upsert_chunks(chunks)
    vs.delete_by_doc("d1")
    assert vs.count() == 1
