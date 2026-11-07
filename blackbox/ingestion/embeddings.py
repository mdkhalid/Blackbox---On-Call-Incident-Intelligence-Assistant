"""Embedding providers."""
from __future__ import annotations

from typing import Protocol


class EmbeddingProvider(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...
    @property
    def dim(self) -> int: ...
    @property
    def name(self) -> str: ...


class FakeEmbeddingProvider:
    """Deterministic hash-based embeddings for tests (no network)."""

    def __init__(self, dim: int = 8):
        self._dim = dim

    def embed(self, texts):
        out = []
        for t in texts:
            vec = [0.0] * self._dim
            for i, ch in enumerate(t):
                vec[i % self._dim] += (ord(ch) % 31) / 31.0
            # normalize
            norm = sum(v * v for v in vec) ** 0.5 or 1.0
            out.append([v / norm for v in vec])
        return out

    @property
    def dim(self):
        return self._dim

    @property
    def name(self):
        return "fake"


class OpenAIEmbeddingProvider:
    def __init__(self, model: str = "text-embedding-3-small", api_key: str | None = None):
        from openai import OpenAI

        self.model = model
        self.client = OpenAI(api_key=api_key)
        self._dim = 1536 if "small" in model else 3072

    def embed(self, texts):
        resp = self.client.embeddings.create(model=self.model, input=texts)
        return [d.embedding for d in resp.data]

    @property
    def dim(self):
        return self._dim

    @property
    def name(self):
        return self.model


class SentenceTransformerEmbeddingProvider:
    def __init__(self, model: str = "all-MiniLM-L6-v2"):
        from sentence_transformers import SentenceTransformer

        self.model_name = model
        self.model = SentenceTransformer(model)

    def embed(self, texts):
        return self.model.encode(texts, convert_to_numpy=True).tolist()

    @property
    def dim(self):
        return self.model.get_sentence_embedding_dimension()

    @property
    def name(self):
        return self.model_name
