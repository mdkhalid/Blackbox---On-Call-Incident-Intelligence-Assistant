"""Token counting and truncation shared by memory and budgeting.

tiktoken is used when available; otherwise a conservative character estimate keeps the
offline path working. All budgeting decisions go through these two functions so the
reported numbers and the enforced limits cannot disagree.
"""
from __future__ import annotations

from functools import lru_cache


@lru_cache(maxsize=1)
def _encoder():
    try:
        import tiktoken

        return tiktoken.get_encoding("cl100k_base")
    except Exception:  # noqa: BLE001 - offline fallback is intentional
        return None


def count_tokens(text: str) -> int:
    if not text:
        return 0
    encoder = _encoder()
    if encoder is not None:
        return len(encoder.encode(text))
    return max(1, len(text) // 4)


def truncate_to_tokens(text: str, limit: int) -> str:
    if not text or limit <= 0:
        return ""
    encoder = _encoder()
    if encoder is not None:
        tokens = encoder.encode(text)
        if len(tokens) <= limit:
            return text
        return encoder.decode(tokens[:limit]).strip()
    if len(text) // 4 <= limit:
        return text
    return text[: limit * 4].strip()
