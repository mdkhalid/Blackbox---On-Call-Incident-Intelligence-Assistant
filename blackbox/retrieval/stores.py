"""Store protocols for the two retrieval legs.

The hybrid retriever talks to two *stores*, not to two concrete implementations. The
protocols are what let the sparse leg move out of the process without touching the
fusion, the rerank, the graph or the API: the in-process BM25 index is the
development adapter, and OpenSearch is the adapter that actually scales.

The metadata filter is a store-neutral AST (`MetadataFilter` in
`blackbox.retrieval.filters`). Each store compiles it into its own predicate
language, so filtering always happens inside the store rather than by post-filtering
a candidate set in Python.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol, runtime_checkable

from blackbox.ingestion.models import Chunk
from blackbox.retrieval.filters import MetadataFilter
from blackbox.retrieval.models import RetrievedChunk


@runtime_checkable
class DenseStore(Protocol):
    """Vector search over a chunk collection."""

    def search(
        self,
        query: str,
        top_k: int,
        filter: MetadataFilter | None = None,
    ) -> list[RetrievedChunk]: ...


@runtime_checkable
class SparseStore(Protocol):
    """Lexical search over a chunk collection.

    `rebuild` exists so the corpus-derived index can be rebuilt off the request path:
    the ingestion worker rebuilds it and publishes it, and the request path only ever
    reads an index that already exists.

    `upsert_chunks` and `delete_by_doc` are the write path. The ingestion pipeline
    writes the dense collection and the sparse index together, so the two never drift.
    """

    def search(
        self,
        query: str,
        top_k: int,
        filter: MetadataFilter | None = None,
    ) -> list[RetrievedChunk]: ...

    def get_chunk(self, chunk_id: str) -> RetrievedChunk | None: ...

    def upsert_chunks(self, chunks: Iterable[Chunk]) -> int: ...

    def delete_by_doc(self, doc_id: str) -> None: ...

    def invalidate(self) -> None: ...

    def warm_up(self) -> None: ...

    def rebuild(self) -> None: ...

    @property
    def size(self) -> int: ...


def opensearch_clauses(filter: MetadataFilter | None) -> list[dict[str, Any]]:
    """Compile a metadata filter into OpenSearch filter clauses.

    List-valued metadata (`services`, `error_codes`) is matched with a `terms` query
    over the keyword field, which is the same "matches any requested value" rule the
    Chroma `$contains` compilation and the in-process matcher implement.
    """
    if filter is None:
        return []
    normalized = filter.normalized()
    clauses: list[dict[str, Any]] = []
    for field, values in (
        ("doc_type", normalized.doc_types),
        ("doc_id", normalized.doc_ids),
        ("severity", normalized.severities),
        ("section", normalized.sections),
        ("services", normalized.services),
        ("error_codes", normalized.error_codes),
    ):
        if values:
            clauses.append({"terms": {field: values}})
    start = _date_ord(normalized.date_from)
    if start is not None:
        clauses.append({"range": {"date_ord": {"gte": start}}})
    end = _date_ord(normalized.date_to)
    if end is not None:
        clauses.append({"range": {"date_ord": {"lte": end}}})
    return clauses


def _date_ord(value: str | None) -> int | None:
    if not value:
        return None
    from blackbox.ingestion.chunkers.base import to_epoch

    return to_epoch(str(value)[:10])
