"""OpenSearch adapter for the sparse leg.

OpenSearch is the decided target for lexical retrieval at scale: the BM25 index lives
in the cluster instead of in every pod's memory, and the metadata filter is compiled
into the query rather than applied to a candidate set in Python.

The adapter is behind the same `SparseStore` protocol as the in-process BM25 index, so
the fusion, the rerank, the graph and the API do not know which one is in use.

**Token parity is deliberate.** Lucene's standard analyzer splits `ERR_POOL_EXHAUSTED`
into three tokens, which would break the one property this retriever exists for:
`ERR_AUTH_FAILED` and `ERR_AUTH_TIMEOUT` must not match each other through a shared
`err`/`auth` fragment. So the chunks are indexed with a pre-tokenized `tokens` field
built by the *same* `tokenize` function the BM25 adapter uses, and the query is
tokenized the same way. The two adapters therefore agree on which documents match,
which is what the contract test asserts.

The client library is imported lazily: the package is an optional dependency, and the
offline path must work without it.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from blackbox.ingestion.models import Chunk
from blackbox.logging_setup import get_logger
from blackbox.retrieval.filters import MetadataFilter
from blackbox.retrieval.models import RetrievedChunk
from blackbox.retrieval.sparse import tokenize
from blackbox.retrieval.stores import opensearch_clauses

logger = get_logger(__name__)

# Fields that the metadata filter compiles against. They are lifted to the top level of
# the document so the compiled clauses can address them directly.
FILTER_FIELDS = (
    "doc_type",
    "doc_id",
    "severity",
    "section",
    "services",
    "error_codes",
    "tenant_id",
)

INDEX_MAPPING: dict[str, Any] = {
    "settings": {
        "index": {"number_of_shards": 1, "number_of_replicas": 0},
        # The BM25 parameters of the in-process adapter, so the two score the same
        # corpus the same way. `rank_bm25` defaults to k1=1.5, b=0.75; Lucene's
        # default is k1=1.2, which would order the results differently.
        "similarity": {"blackbox_bm25": {"type": "BM25", "k1": 1.5, "b": 0.75}},
    },
    "mappings": {
        "properties": {
            "chunk_id": {"type": "keyword"},
            # The pre-tokenized text. The whitespace analyzer keeps each token whole, so
            # the indexed terms are exactly the terms the BM25 adapter scores.
            "tokens": {
                "type": "text",
                "analyzer": "whitespace",
                "similarity": "blackbox_bm25",
            },
            # The raw text, kept for display and for the standard-analyzed secondary
            # match.
            "text": {"type": "text", "analyzer": "standard"},
            "doc_type": {"type": "keyword"},
            "doc_id": {"type": "keyword"},
            "severity": {"type": "keyword"},
            "section": {"type": "keyword"},
            "services": {"type": "keyword"},
            "error_codes": {"type": "keyword"},
            "tenant_id": {"type": "keyword"},
            "date_ord": {"type": "long"},
            # Stored but not indexed: it is only ever read back for the trace viewer.
            "metadata": {"type": "object", "enabled": False},
        }
    },
}


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
        tokens_field: str = "tokens",
        chunk_id_field: str = "chunk_id",
        alias: str | None = None,
    ) -> None:
        self.hosts = [host.strip() for host in hosts.split(",") if host.strip()]
        # When an alias is supplied, `index` names the read alias and concrete
        # indexes are versioned ({alias}-vN) behind it. Otherwise `index` is a
        # concrete index, which keeps the development adapter simple.
        self.alias = alias
        self.index = alias if alias else index
        self.username = username
        self.password = password
        self.verify_certs = verify_certs
        self.ca_certs = ca_certs
        self.text_field = text_field
        self.tokens_field = tokens_field
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

    async def asearch(
        self,
        query: str,
        top_k: int,
        filter: MetadataFilter | None = None,
    ) -> list[RetrievedChunk]:
        import asyncio

        return await asyncio.to_thread(self.search, query, top_k, filter)

    def rebuild(self) -> None:
        """Nothing to rebuild: the index is maintained by the cluster."""

    def ensure_index(self) -> None:
        """Create the index with its mapping if it does not exist yet."""
        if self.alias:
            if not self.list_versions():
                self.create_versioned(1)
                self.swap_alias(1)
            return
        if self.client.indices.exists(index=self.index):
            return
        self.client.indices.create(index=self.index, body=INDEX_MAPPING)
        logger.info("opensearch_index_created", index=self.index)

    def drop_index(self) -> None:
        """Delete the index. Used by the tests and by a full reindex."""
        if self.alias:
            for v in self.list_versions():
                name = self.versioned_index_name(v)
                if self.client.indices.exists(index=name):
                    self.client.indices.delete(index=name)
            return
        if self.client.indices.exists(index=self.index):
            self.client.indices.delete(index=self.index)

    # ---- versioned indexes / alias cutover (Phase 2) ----

    def versioned_index_name(self, version: int) -> str:
        """Concrete index name for a given version."""
        return f"{self.index}-v{version}"

    def list_versions(self) -> list[int]:
        """Return the versions of concrete indexes behind the alias."""
        versions: list[int] = []
        try:
            for name in self.client.indices.get_alias(index=f"{self.index}-v*"):
                suffix = name.rsplit("-v", 1)[-1]
                if suffix.isdigit():
                    versions.append(int(suffix))
        except Exception:  # noqa: BLE001
            return versions
        return sorted(versions)

    def create_versioned(self, version: int) -> str:
        """Create a concrete versioned index with the standard mapping."""
        name = self.versioned_index_name(version)
        if not self.client.indices.exists(index=name):
            self.client.indices.create(index=name, body=INDEX_MAPPING)
        return name

    def swap_alias(self, version: int) -> None:
        """Atomically point the read alias at a concrete versioned index."""
        target = self.versioned_index_name(version)
        actions: list[dict[str, Any]] = []
        try:
            current = self.client.indices.get_alias(name=self.index)
            for old in current:
                actions.append({"remove": {"index": old, "alias": self.index}})
        except Exception:  # noqa: BLE001, S110
            pass
        actions.append({"add": {"index": target, "alias": self.index}})
        self.client.indices.update_aliases({"actions": actions})

    @property
    def size(self) -> int:
        try:
            response = self.client.count(index=self.index)
        except Exception as exc:  # noqa: BLE001 - an unreachable cluster is not fatal
            logger.warning("opensearch_count_failed", error=str(exc))
            return 0
        return int(response.get("count", 0))

    # ---- write path ----

    def upsert_chunks(self, chunks: Iterable[Chunk]) -> int:
        """Index chunks into the search index.

        The index is refreshed before returning, so a search that follows an ingest
        sees the new chunks. That costs a refresh per call, which is the right trade
        for an ingest that runs on a background job and must be searchable when it
        reports completion.
        """
        from opensearchpy import helpers

        batch = list(chunks)
        if not batch:
            return 0
        self.ensure_index()
        actions = [self._action(chunk) for chunk in batch]
        success, _failed = helpers.bulk(self.client, actions, chunk_size=500, refresh=True)
        return int(success)

    def delete_by_doc(self, doc_id: str) -> None:
        """Remove every chunk belonging to a document."""
        if not self.client.indices.exists(index=self.index):
            return
        self.client.delete_by_query(
            index=self.index,
            body={"query": {"term": {"doc_id": doc_id}}},
            refresh=True,
        )

    def _action(self, chunk: Chunk) -> dict[str, Any]:
        metadata = dict(chunk.metadata or {})
        metadata.setdefault("tenant_id", "default")
        document: dict[str, Any] = {
            self.chunk_id_field: chunk.chunk_id,
            self.tokens_field: " ".join(tokenize(chunk.text or "")),
            self.text_field: chunk.text or "",
            "metadata": metadata,
        }
        for field in FILTER_FIELDS:
            value = metadata.get(field)
            if value is None:
                continue
            # The value keeps its shape: a scalar stays a scalar and a list stays a
            # list. A `terms` query matches either form.
            document[field] = value
        date_ord = metadata.get("date_ord")
        if date_ord is not None:
            document["date_ord"] = int(date_ord)
        return {
            "_op_type": "index",
            "_index": self.index,
            "_id": chunk.chunk_id,
            "_source": document,
        }

    # ---- search ----

    def search(
        self,
        query: str,
        top_k: int,
        filter: MetadataFilter | None = None,
    ) -> list[RetrievedChunk]:
        response = self.client.search(
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
        """Build the query body.

        The query is tokenized with the same function the index used, so the terms the
        cluster scores are exactly the terms the BM25 adapter would have scored.
        """
        tokens = tokenize(query or "")
        body: dict[str, Any] = {
            "size": top_k,
            "query": {
                "bool": {
                    "must": [
                        {"match": {self.tokens_field: " ".join(tokens)}},
                    ]
                }
            },
            # Equal scores are broken by document id and then by chunk id, both taken
            # from the chunk itself. The in-process adapter computes the same order,
            # so the two backends rank identically.
            "sort": [
                {"_score": {"order": "desc"}},
                {"doc_id": {"order": "asc", "missing": "_last"}},
                {self.chunk_id_field: {"order": "asc"}},
            ],
        }
        if not tokens:
            # An empty query would match everything; an empty filter clause must not.
            body["query"]["bool"]["must"] = [{"match_none": {}}]
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
