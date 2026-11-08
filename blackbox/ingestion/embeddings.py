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


class OnnxEmbeddingProvider:
    """Chroma's bundled ONNX MiniLM model: real embeddings with no API key and no torch.

    Used as the local fallback so dense retrieval and the eval harness produce genuine
    numbers offline. Model weights are downloaded once and cached by Chroma.
    """

    def __init__(self, model: str = "all-MiniLM-L6-v2", *, warm_up: bool = True):
        from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

        self.model_name = model
        self._fn = DefaultEmbeddingFunction()
        self._dim = 384
        if warm_up:
            self.warm_up()

    def warm_up(self) -> None:
        """Load the model and run one inference.

        Loading the ONNX graph takes longer than the per-request retrieval budget on a cold
        process. Doing it here means the first user request is not the one that pays for it,
        which matters because the retrieval deadline would otherwise abort a perfectly
        healthy first query.
        """
        self._fn(["warm up"])

    def embed(self, texts):
        out = self._fn(list(texts))
        return [v.tolist() if hasattr(v, "tolist") else list(v) for v in out]

    @property
    def dim(self):
        return self._dim

    @property
    def name(self):
        return f"onnx:{self.model_name}"


def build_embedding_provider(
    provider: str,
    *,
    model: str = "text-embedding-3-small",
    api_key: str | None = None,
    dim: int = 8,
) -> EmbeddingProvider:
    """Construct an embedding provider by name.

    `auto` prefers a real semantic model and degrades to ONNX MiniLM when no API key is
    configured, so a fresh clone can still run retrieval and eval offline.
    """
    choice = (provider or "auto").lower()
    if choice == "fake":
        return FakeEmbeddingProvider(dim=dim)
    if choice == "openai":
        return OpenAIEmbeddingProvider(model=model, api_key=api_key)
    if choice == "sentence_transformer":
        return SentenceTransformerEmbeddingProvider(model="all-MiniLM-L6-v2")
    if choice == "onnx":
        return OnnxEmbeddingProvider()
    if choice == "auto":
        if api_key:
            return OpenAIEmbeddingProvider(model=model, api_key=api_key)
        return OnnxEmbeddingProvider()
    raise ValueError(f"unknown embedding provider: {provider}")
