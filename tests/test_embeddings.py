from blackbox.ingestion.embeddings import FakeEmbeddingProvider


def test_fake_embedding_dim():
    p = FakeEmbeddingProvider(dim=4)
    vecs = p.embed(["hello", "world"])
    assert len(vecs) == 2
    assert len(vecs[0]) == 4
    # normalized
    import math

    norm = math.sqrt(sum(v * v for v in vecs[0]))
    assert abs(norm - 1.0) < 1e-6


def test_fake_embedding_deterministic():
    p = FakeEmbeddingProvider(dim=4)
    a = p.embed(["abc"])[0]
    b = p.embed(["abc"])[0]
    assert a == b
