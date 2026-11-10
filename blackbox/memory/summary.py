"""Summary memory: a cumulative, incident-aware compression of older turns."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SummaryMemory:
    text: str = ""
    turns_since_compression: int = 0
    compression_count: int = 0
