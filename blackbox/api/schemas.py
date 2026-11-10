"""Request models for the public API."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    conversation_id: str | None = None
    message: str
    filters: dict[str, Any] | None = None


class IngestRequest(BaseModel):
    corpus_dir: str = "seed/corpus"
    refresh: bool = False


class EvalRequest(BaseModel):
    k: int = Field(default=5, ge=1, le=50)
    fusion_sweep: bool = False
    groundedness: bool = False
    limit: int | None = Field(default=None, ge=1, le=100)
