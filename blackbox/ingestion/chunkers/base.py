"""Base chunker: section-aware splitting helpers."""
from __future__ import annotations

from collections.abc import Iterator

from blackbox.ingestion.models import Chunk, Document, make_chunk_id
from blackbox.ingestion.tokenizer import count_tokens


def split_text(text: str, max_tokens: int) -> list[str]:
    """Split text into pieces <= max_tokens, preferring paragraph then sentence boundaries."""
    text = text.strip()
    if not text:
        return []
    if count_tokens(text) <= max_tokens:
        return [text]
    # split by paragraphs
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    pieces: list[str] = []
    buf: list[str] = []
    buf_tokens = 0
    for p in paras:
        pt = count_tokens(p)
        if pt > max_tokens:
            # flush buf
            if buf:
                pieces.append("\n\n".join(buf))
                buf = []
                buf_tokens = 0
            # split paragraph by sentences
            sents = _split_sentences(p)
            for s in sents:
                if count_tokens(s) > max_tokens:
                    # hard split by words
                    words = s.split()
                    chunk_words: list[str] = []
                    for w in words:
                        chunk_words.append(w)
                        if count_tokens(" ".join(chunk_words)) >= max_tokens:
                            pieces.append(" ".join(chunk_words))
                            chunk_words = []
                    if chunk_words:
                        pieces.append(" ".join(chunk_words))
                else:
                    if buf and buf_tokens + count_tokens(s) > max_tokens:
                        pieces.append("\n".join(buf))
                        buf = []
                        buf_tokens = 0
                    buf.append(s)
                    buf_tokens += count_tokens(s)
            if buf:
                pieces.append("\n".join(buf))
                buf = []
                buf_tokens = 0
        else:
            if buf and buf_tokens + pt > max_tokens:
                pieces.append("\n\n".join(buf))
                buf = []
                buf_tokens = 0
            buf.append(p)
            buf_tokens += pt
    if buf:
        pieces.append("\n\n".join(buf))
    return [p for p in pieces if p.strip()]


def _split_sentences(text: str) -> list[str]:
    import re

    parts = re.split(r"(?<=[.!?])\s+", text)
    return [p.strip() for p in parts if p.strip()]


def _as_str_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [str(v).strip() for v in value if str(v).strip()]


def _as_iso_date(value) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()[:10]
    return str(value).strip()[:10] or None


def to_epoch(date_str: str | None) -> int | None:
    """Convert an ISO date to an integer epoch day so range filters work in Chroma."""
    if not date_str:
        return None
    try:
        import datetime as _dt

        return int(_dt.date.fromisoformat(date_str).toordinal())
    except ValueError:
        return None


def make_metadata(doc: Document, section: str, extra: dict | None = None) -> dict:
    """Build chunk metadata.

    Lists stay lists (Chroma `$contains` filtering depends on it) and empty values are
    omitted entirely, because Chroma rejects empty list metadata values.
    """
    date = _as_iso_date(doc.metadata.get("date"))
    services = _as_str_list(doc.metadata.get("services"))
    error_codes = _as_str_list(doc.metadata.get("error_codes"))

    md: dict = {
        "doc_id": doc.doc_id,
        "doc_type": doc.doc_type,
        "title": doc.title,
        "section": section,
    }
    if services:
        md["services"] = services
    if error_codes:
        md["error_codes"] = error_codes
    if doc.metadata.get("severity"):
        md["severity"] = str(doc.metadata["severity"])
    if date:
        md["date"] = date
        date_ord = to_epoch(date)
        if date_ord is not None:
            md["date_ord"] = date_ord
    if extra:
        md.update(extra)
    return md


def chunk_sections(
    doc: Document, max_tokens: int, section_max: dict | None = None
) -> Iterator[Chunk]:
    """Yield chunks per section, splitting oversized sections."""
    for section, text in doc.sections.items():
        if not text.strip():
            continue
        limit = (section_max or {}).get(section, max_tokens)
        parts = split_text(text, limit)
        for i, part in enumerate(parts):
            cid = make_chunk_id(doc.doc_id, section, i)
            md = make_metadata(doc, section, {"part": i, "tokens": count_tokens(part)})
            yield Chunk(chunk_id=cid, text=part, metadata=md)
