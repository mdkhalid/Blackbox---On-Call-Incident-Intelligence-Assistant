"""OpenSearch adapter for the sparse leg.

OpenSearch is the decided target for lexical retrieval at scale: the BM25 index lives
in the cluster instead of in every pod's memory, and the metadata filter is compiled
into the query rather than applied to a candidate set in Python.

The adapter is behind the same `SparseStore` protocol as the in-process BM25 index, so
the fusion, the rerank, the graph and the API do not know which one is in use.

The client library is imported lazily: the package is an optional dependency, and the
offline path must work without it.
"""
from __future__ import annotations

from typing import Any

from blackbox.logging_setup import get_logger
from blackbox.retrieval.filters import MetadataFilter
from blackbox.retrieval.models import RetrievedChunk
from blackbox.retrieval.stores import opensearch_clauses

logger = get_logger(__name__)


class OpenSearchUnavailable(RuntimeError):
    """The OpenSearch client library or the cluster is not reachable."""


class OpenSearchSparseStore:
    """BM25 retrieval over an OpenSearch index."""

    def __init__(
        self,
        *,
        hosts: str,
        index: str = "blackbox_chunks",
        username: str = "",
        password: str = "",
        verify_certs: bool = True,
        ca_certs: str | None = None,
        text_field: str = "text",
        chunk_id_field: str = "chunk_id",
    ) -> None:
        self.hosts = [host.strip() for host in hosts.split(",") if host.strip()]
        self.index = index
        self.username = username
        self.password = password
        self.verify_certs = verify_certs
        self.ca_certs = ca_certs
        self.text_field = text_field
        self.chunk_id_field = chunk_id_field
        self._client: Any = None
        self._async_client: Any = None

    # ---- connection ----

    def _build_client(self) -> Any:
        try:
            from opensearchpy import OpenSearch
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise OpenSearchUnavailable(
                "the opensearch-py package is required for the OpenSearch sparse backend"
            ) from exc
        auth = (self.username, self.password) if self.username else None
        return OpenSearch(
            hosts=self.hosts,
            http_auth=auth,
            verify_certs=self.verify_certs,
            ca_certs=self.ca_certs,
        )

    def _build_async_client(self) -> Any:
        try:
            from opensearchpy import AsyncOpenSearch
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise OpenSearchUnavailable(
                "the opensearch-py package is required for the OpenSearch sparse backend"
            ) from exc
        auth = (self.username, self.password) if self.username else None
        return AsyncOpenSearch(
            hosts=self.hosts,
            http_auth=auth,
            verify_certs=self.verify_certs,
            ca_certs=self.ca_certs,
        )

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = self._build_client()
        return self._client

    @property
    def async_client(self) -> Any:
        if self._async_client is None:
            self._async_client = self._build_async_client()
        return self._async_client

    # ---- index management ----

    def invalidate(self) -> None:
        """Nothing to invalidate: the index lives in the cluster, not in this process."""

    def warm_up(self) -> None:
        """Nothing to warm up: there is no in-process index."""

    def rebuild(self) -> None:
        """Nothing to rebuild: the index is maintained by the cluster."""

    @property
    def size(self) -> int:
        try:
            response = self.client.count(index=self.index)
        except Exception as exc:  # noqa: BLE001 - an unreachable cluster is not fatal
            logger.warning("opensearch_count_failed", error=str(exc))
            return 0
        return int(response.get("count", 0))

    # ---- search ----

    def search(
        self,
        query: str,
        top_k: int,
        filter: MetadataFilter | None = None,
    ) -> list[RetrievedChunk]:
        response = self.client.search(index=self.index, body=self._body(query, top_k, filter))
        return self._parse(response)

    async def asearch(
        self,
        query: str,
        top_k: int,
        filter: MetadataFilter | None = None,
    ) -> list[RetrievedChunk]:
        response = await self.async_client.search(
            index=self.index, body=self._body(query, top_k, filter)
        )
        return self._parse(response)

    def get_chunk(self, chunk_id: str) -> RetrievedChunk | None:
        try:
            response = self.client.get(index=self.index, id=chunk_id)
        except Exception:  # noqa: BLE001 - a missing chunk is not an error
            return None
        source = response.get("_source") or {}
        return RetrievedChunk(
            chunk_id=chunk_id,
            text=source.get(self.text_field, ""),
            metadata=dict(source.get("metadata") or {}),
        )

    # ---- helpers ----

    def _body(
        self, query: str, top_k: int, filter: MetadataFilter | None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "size": top_k,
            "query": {
                "bool": {
                    "must": [
                        {
                            "multi_match": {
                                "query": query,
                                "fields": [self.text_field],
                            }
                        }
                    ]
                }
            },
        }
        clauses = opensearch_clauses(filter)
        if clauses:
            body["query"]["bool"]["filter"] = clauses
        return body

    def _parse(self, response: dict[str, Any]) -> list[RetrievedChunk]:
        hits = ((response.get("hits") or {}).get("hits")) or []
        chunks: list[RetrievedChunk] = []
        for rank, hit in enumerate(hits, start=1):
            source = hit.get("_source") or {}
            score = float(hit.get("_score") or 0.0)
            chunks.append(
                RetrievedChunk(
                    chunk_id=str(hit.get("_id") or source.get(self.chunk_id_field) or ""),
                    text=source.get(self.text_field, ""),
                    metadata=dict(source.get("metadata") or {}),
                    sparse_score=score,
                    sparse_rank=rank,
                )
            )
        return chunks
