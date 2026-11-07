"""Base loader utilities: frontmatter + section splitting."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from blackbox.ingestion.models import Document


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split YAML frontmatter from body. Returns (meta, body)."""
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            try:
                meta = yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError:
                meta = {}
            body = parts[2].lstrip("\n")
            return meta, body
    return {}, text


def split_sections(body: str) -> dict[str, str]:
    """Split markdown body into sections keyed by H2 heading (## Title)."""
    sections: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in body.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            current = m.group(1).strip()
            buf = []
        else:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip()
    # content before first heading -> "Intro"
    # (already consumed into first section if heading exists; if none, store as Intro)
    if not sections and body.strip():
        sections["Intro"] = body.strip()
    return sections


def load_document(path: Path, doc_type: str) -> Document:
    text = path.read_text(encoding="utf-8")
    meta, body = parse_frontmatter(text)
    sections = split_sections(body)
    doc_id = meta.get("id") or path.stem
    title = meta.get("title") or (next(iter(sections.keys())) if sections else path.stem)
    metadata = {k: v for k, v in meta.items() if k not in ("id", "title")}
    metadata.setdefault("doc_type", doc_type)
    return Document(doc_id=doc_id, doc_type=doc_type, title=title, metadata=metadata, sections=sections, raw=text)
