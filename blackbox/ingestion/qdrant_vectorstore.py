"""Qdrant vector store adapter (optional dense backend).

Mirrors the :class:`ChromaVectorStore` interface so it can be dropped in without
touching the retrieval pipeline. qdrant-client is an optional dependency; import
failures are deferred to construction time so the rest of the package imports
cleanly without it installed.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any


class QdrantVectorStore:
    def __init__(
        self,
        url: str = "http://localhost:6333",
        collection_name: str = "blackbox_chunks",
        embedding_provider: Any = None,
        api_key: str | None = None,
        prefer_grpc: bool = False,
    ):
        try:
            from qdrant_client import QdrantClient
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise ImportError(
                "qdrant-client is required for the Qdrant backend; "
                "install with `pip install -e '.[vector]'`"
            ) from exc

        self.collection_name = collection_name
        self.embedding_provider = embedding_provider
        if url == ":memory:":
            self._client = QdrantClient(location=":memory:")
        else:
            self._client = QdrantClient(url=url, api_key=api_key, prefer_grpc=prefer_grpc)
        self._collection_ready = False
        self._vector_size: int | None = None

    def _ensure_collection(self, vector_size: int) -> None:
        if self._collection_ready and self._vector_size == vector_size:
            return
        from qdrant_client.http import models as qm

        try:
            self._client.get_collection(self.collection_name)
        except Exception:  # noqa: BLE001 - collection may not exist yet
            self._client.create_collection(
                collection_name=self.collection_name,
                vectors_config=qm.VectorParams(size=vector_size, distance=qm.Distance.COSINE),
            )
        self._vector_size = vector_size
        self._collection_ready = True

    def upsert_chunks(self, chunks: Iterable) -> int:
        from qdrant_client.http import models as qm

        ids = []
        docs = []
        metas = []
        for c in chunks:
            ids.append(c.chunk_id)
            docs.append(c.text)
            meta = dict(c.metadata or {})
            meta.setdefault("tenant_id", "default")
            metas.append(meta)
        if not ids:
            return 0
        import uuid as _uuid

        embeddings = self.embedding_provider.embed(docs)
        self._ensure_collection(len(embeddings[0]))
        point_ids = [str(_uuid.uuid5(_uuid.NAMESPACE_URL, str(cid))) for cid in ids]
        points = [
            qm.PointStruct(
                id=point_ids[i],
                vector=embeddings[i],
                payload={"text": docs[i], "metadata": metas[i], "chunk_id": ids[i]},
            )
            for i in range(len(ids))
        ]
        self._client.upsert(collection_name=self.collection_name, points=points)
        return len(ids)

    def delete_by_doc(self, doc_id: str) -> None:
        from qdrant_client.http import models as qm

        try:
            self._client.delete(
                collection_name=self.collection_name,
                points_selector=qm.FilterSelector(
                    filter=qm.Filter(
                        must=[qm.FieldCondition(key="metadata.doc_id", match=qm.MatchValue(value=doc_id))]
                    )
                ),
            )
        except Exception:  # noqa: BLE001, S110
            pass

    def count(self) -> int:
        try:
            info = self._client.get_collection(self.collection_name)
            return int(info.points_count or 0)
        except Exception:  # noqa: BLE001
            return 0

    def has_doc(self, doc_id: str) -> bool:
        from qdrant_client.http import models as qm

        try:
            res, _ = self._client.scroll(
                collection_name=self.collection_name,
                scroll_filter=qm.Filter(
                    must=[qm.FieldCondition(key="metadata.doc_id", match=qm.MatchValue(value=doc_id))]
                ),
                limit=1,
                with_payload=False,
                with_vectors=False,
            )
            return bool(res)
        except Exception:  # noqa: BLE001
            return False

    def query_embedding(self, embedding, top_k=5, where=None, filter=None):
        from qdrant_client.http import models as qm

        qfilter = None
        if filter is not None:
            qfilter = self._compile_filter(filter, qm)
        elif where:
            # `where` is a Chroma-style dict; best-effort translate scalar equality.
            must = []
            for key, val in (where.items() if isinstance(where, dict) else []):
                if isinstance(val, dict):
                    continue
                must.append(qm.FieldCondition(key=f"metadata.{key}", match=qm.MatchValue(value=val)))
            if must:
                qfilter = qm.Filter(must=must)
        if hasattr(self._client, "search"):
            results = self._client.search(
                collection_name=self.collection_name, query_vector=embedding, limit=top_k, query_filter=qfilter
            )
        else:
            resp = self._client.query_points(
                collection_name=self.collection_name, query=embedding, limit=top_k, query_filter=qfilter
            )
            results = resp.points
        ids = [[(r.payload or {}).get("chunk_id", r.id) for r in results]]
        docs = [[(r.payload or {}).get("text", "") for r in results]]
        metas = [[(r.payload or {}).get("metadata", {}) for r in results]]
        dists = [[1.0 - float(r.score) for r in results]]
        return {"ids": ids, "documents": docs, "metadatas": metas, "distances": dists}

    def _compile_filter(self, f, qm):
        try:
            nf = f.normalized() if hasattr(f, "normalized") else f
        except Exception:  # noqa: BLE001
            nf = f
        must = []
        # tenant is mandatory
        tenant = getattr(nf, "tenant_id", None)
        if not tenant:
            tenant = "default"
        must.append(qm.FieldCondition(key="metadata.tenant_id", match=qm.MatchValue(value=str(tenant))))
        for field_name, values in (
            ("doc_type", getattr(nf, "doc_types", None)),
            ("doc_id", getattr(nf, "doc_ids", None)),
            ("severity", getattr(nf, "severities", None)),
            ("section", getattr(nf, "sections", None)),
        ):
            vals = values or []
            if len(vals) == 1:
                must.append(qm.FieldCondition(key=f"metadata.{field_name}", match=qm.MatchValue(value=vals[0])))
            elif vals:
                must.append(qm.FieldCondition(key=f"metadata.{field_name}", match=qm.MatchAny(any=list(vals))))
        for field_name, values in (
            ("services", getattr(nf, "services", None)),
            ("error_codes", getattr(nf, "error_codes", None)),
        ):
            vals = values or []
            if vals:
                must.append(qm.FieldCondition(key=f"metadata.{field_name}", match=qm.MatchAny(any=list(vals))))
        # date range
        start = getattr(nf, "date_from", None)
        end = getattr(nf, "date_to", None)
        if start is not None or end is not None:
            try:
                from blackbox.ingestion.chunkers.base import to_epoch

                start_ord = to_epoch(str(start)[:10]) if start else None
                end_ord = to_epoch(str(end)[:10]) if end else None
            except Exception:  # noqa: BLE001
                start_ord, end_ord = None, None
            rng = {}
            if start_ord is not None:
                rng["gte"] = start_ord
            if end_ord is not None:
                rng["lte"] = end_ord
            if rng:
                must.append(qm.FieldCondition(key="metadata.date_ord", range=qm.Range(**rng)))
        return qm.Filter(must=must) if must else None

    def create_snapshot(self) -> str | None:
        """Create a snapshot of the collection. Returns the snapshot name or None."""
        try:
            info = self._client.create_snapshot(collection_name=self.collection_name, wait=True)
            return getattr(info, "name", None)
        except Exception:  # noqa: BLE001
            return None

    def list_snapshots(self) -> list[str]:
        try:
            snaps = self._client.list_snapshots(collection_name=self.collection_name)
            return [getattr(s, "name", str(s)) for s in (snaps or [])]
        except Exception:  # noqa: BLE001
            return []

    def get_all(self, where=None):
        points, _ = self._client.scroll(
            collection_name=self.collection_name, limit=10_000, with_payload=True, with_vectors=False
        )
        ids = [p.id for p in points]
        docs = [(p.payload or {}).get("text", "") for p in points]
        metas = [(p.payload or {}).get("metadata", {}) for p in points]
        return {"ids": ids, "documents": docs, "metadatas": metas}