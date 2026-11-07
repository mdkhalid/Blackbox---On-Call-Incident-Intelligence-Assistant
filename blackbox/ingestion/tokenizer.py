"""Token counting utility."""
from __future__ import annotations

try:
    import tiktoken

    _enc = tiktoken.get_encoding("cl100k_base")

    def count_tokens(text: str) -> int:
        return len(_enc.encode(text))
except ImportError:  # pragma: no cover - fallback
    def count_tokens(text: str) -> int:
        return max(1, int(len(text.split()) * 1.3))
