"""SSE helpers shared by the chat route."""
from __future__ import annotations

import json
from typing import Any

_CHUNK_FIELDS = (
    "chunk_id",
    "doc_id",
    "section",
    "doc_type",
    "dense_score",
    "dense_rank",
    "sparse_score",
    "sparse_rank",
    "fused_score",
    "fused_rank",
    "rerank_score",
    "rerank_rank",
    "score",
)


def sse_event(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def token_chunks(text: str, size: int = 24) -> list[str]:
    """Split the final, ground-checked answer into transport-sized pieces.

    These are not raw decode tokens: the ground check must run before any answer text is
    exposed, so token events intentionally start after `ground_check`/`honest_failure`.
    """
    return [text[i : i + size] for i in range(0, len(text), size)]


def chunk_trails(chunks: list[dict]) -> list[dict]:
    return [{key: chunk.get(key) for key in _CHUNK_FIELDS} for chunk in chunks]


def public_update(values: dict) -> dict:
    """Node update safe to put on the wire: no chunk text, no duplicated trace."""
    out: dict[str, Any] = {}
    for key, value in values.items():
        if key in ("trace", "answer"):
            continue
        if key == "chunks" and isinstance(value, list):
            out[key] = chunk_trails(value)
        else:
            out[key] = value
    return out
