"""Core data models for ingestion."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

# Default tenant used when no tenant is supplied (dev/offline runs). Production
# deployments always set a real tenant_id; an empty tenant is rejected everywhere.
DEFAULT_TENANT = "default"


@dataclass
class Document:
    doc_id: str
    doc_type: str  # postmortem | runbook | arch | alert
    title: str
    metadata: dict
    sections: dict[str, str]  # section name -> text
    raw: str = ""


@dataclass
class Chunk:
    chunk_id: str
    text: str
    metadata: dict


@dataclass
class ExtractedEntities:
    services: list[str] = field(default_factory=list)
    error_codes: list[str] = field(default_factory=list)
    dependencies: list[tuple[str, str]] = field(default_factory=list)


def make_chunk_id(doc_id: str, section: str, part: int = 0) -> str:
    h = hashlib.sha256(f"{doc_id}::{section}::{part}".encode()).hexdigest()
    return h
