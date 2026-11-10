"""Ingestion pipeline: load -> chunk -> extract entities -> embed/upsert -> track."""
from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

from blackbox.ingestion.chunkers import (
    chunk_alert,
    chunk_arch,
    chunk_postmortem,
    chunk_runbook,
)
from blackbox.ingestion.entity_extraction import extract_entities
from blackbox.ingestion.entity_extraction.store import SqlAlchemyEntityStore
from blackbox.ingestion.loaders import (
    load_alert,
    load_arch,
    load_postmortem,
    load_runbook,
)
from blackbox.ingestion.vectorstore import ChromaVectorStore

CHUNKER_MAP = {
    "postmortem": (load_postmortem, chunk_postmortem),
    "runbook": (load_runbook, chunk_runbook),
    "arch": (load_arch, chunk_arch),
    "alert": (load_alert, chunk_alert),
}

LOADER_BY_DIR = {
    "postmortems": "postmortem",
    "runbooks": "runbook",
    "arch": "arch",
    "alerts": "alert",
}


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class IngestionPipeline:
    def __init__(self, vectorstore: ChromaVectorStore, entity_store: SqlAlchemyEntityStore, batch_size: int = 64):
        self.vs = vectorstore
        self.es = entity_store
        self.batch_size = batch_size

    def _loader_chunker(self, doc_type: str):
        # find in CHUNKER_MAP by doc_type
        for dt, (ld, ch) in CHUNKER_MAP.items():
            if dt == doc_type:
                return ld, ch
        raise ValueError(f"unknown doc_type: {doc_type}")

    def ingest_document(self, path: Path, doc_type: str | None = None) -> dict:
        if doc_type is None:
            # infer from parent dir name
            doc_type = LOADER_BY_DIR.get(path.parent.name, "postmortem")
        loader, chunker = self._loader_chunker(doc_type)
        doc = loader(path)
        # override doc_type if provided
        doc.doc_type = doc_type
        doc.metadata["doc_type"] = doc_type
        h = content_hash(doc.raw)
        existing = self.es.get_ingested(doc.doc_id)
        if (
            existing is not None
            and existing.content_hash == h
            and self.vs.has_doc(doc.doc_id)
        ):
            return {"doc_id": doc.doc_id, "status": "skipped", "chunks": 0}

        # delete old
        if existing is not None:
            self.vs.delete_by_doc(doc.doc_id)
            self.es.delete_doc_entities(doc.doc_id)

        # extract entities
        known_services = doc.metadata.get("services")
        if isinstance(known_services, str):
            known_services = [s.strip() for s in known_services.split(",") if s.strip()]
        full_text = doc.raw
        ents = extract_entities(full_text, known_services=known_services or [])
        # also use metadata services
        for svc in (known_services or []):
            self.es.upsert_service(svc)
        for ec in ents.error_codes:
            self.es.upsert_error_code(ec)
        for src, dst in ents.dependencies:
            self.es.upsert_dependency(src, dst)
        # doc entities
        for svc in (known_services or ents.services):
            self.es.add_doc_entity(doc.doc_id, "service", svc)
        for ec in ents.error_codes:
            self.es.add_doc_entity(doc.doc_id, "error_code", ec)

        # chunk
        chunks = list(chunker(doc))
        # batched upsert
        total = 0
        for i in range(0, len(chunks), self.batch_size):
            batch = chunks[i : i + self.batch_size]
            n = self.vs.upsert_chunks(batch)
            total += n
        self.es.record_ingested(doc.doc_id, h, [c.chunk_id for c in chunks])
        return {"doc_id": doc.doc_id, "status": "ingested", "chunks": total}

    def ingest_directory(self, root: Path, progress: Callable | None = None) -> list[dict]:
        results = []
        for subdir in ("postmortems", "runbooks", "arch", "alerts"):
            d = root / subdir
            if not d.exists():
                continue
            for path in sorted(d.glob("*.md")):
                r = self.ingest_document(path)
                results.append(r)
                if progress:
                    progress(r)
        return results
