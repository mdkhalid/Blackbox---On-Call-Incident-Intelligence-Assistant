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


def make_metadata(doc: Document, section: str, extra: dict | None = None) -> dict:
    md = {
        "doc_id": doc.doc_id,
        "doc_type": doc.doc_type,
        "title": doc.title,
        "section": section,
        "services": doc.metadata.get("services", []),
        "severity": doc.metadata.get("severity"),
        "date": doc.metadata.get("date"),
        "error_codes": doc.metadata.get("error_codes", []),
    }
    if extra:
        md.update(extra)
    # flatten lists to comma strings? Chroma supports list metadata? Chroma metadata values must be str/int/float/bool or None. Lists not allowed. Convert lists to comma-joined strings.
    for k, v in list(md.items()):
        if isinstance(v, list):
            md[k] = ",".join(str(x) for x in v)
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
