"""LangChain callback handler for operator-facing metrics.

The graph also records per-node timings in its trace; these callbacks are for standard
LangChain chains (retrieval composition, future LCEL use) and are emitted as structured
logs, not mixed into the product trace.
"""
from __future__ import annotations

import time
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

from blackbox.logging_setup import get_logger


class LoggingCallbackHandler(BaseCallbackHandler):
    def __init__(self, logger: Any | None = None) -> None:
        self.logger = logger or get_logger("observability")
        self._started: dict[Any, float] = {}

    # ---- llm ----

    def on_llm_start(self, serialized, prompts, *, run_id=None, **kwargs):
        self._started[run_id] = time.perf_counter()

    def on_llm_end(self, response, *, run_id=None, **kwargs):
        duration = self._duration(run_id)
        usage = {}
        llm_output = getattr(response, "llm_output", None) or {}
        if isinstance(llm_output, dict):
            usage = llm_output.get("token_usage") or llm_output.get("usage") or {}
        self.logger.info(
            "llm_call",
            source="callback",
            duration_ms=duration,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
        )

    def on_llm_error(self, error, *, run_id=None, **kwargs):
        self.logger.error(
            "llm_error", source="callback", duration_ms=self._duration(run_id), error=str(error)
        )

    # ---- retrieval ----

    def on_retriever_start(self, serialized, query, *, run_id=None, **kwargs):
        self._started[run_id] = time.perf_counter()

    def on_retriever_end(self, documents, *, run_id=None, **kwargs):
        self.logger.info(
            "retrieval",
            source="callback",
            duration_ms=self._duration(run_id),
            documents=len(documents or []),
        )

    def on_retriever_error(self, error, *, run_id=None, **kwargs):
        self.logger.error(
            "retrieval_error",
            source="callback",
            duration_ms=self._duration(run_id),
            error=str(error),
        )

    # ---- chain ----

    def on_chain_start(self, serialized, inputs, *, run_id=None, **kwargs):
        self._started[run_id] = time.perf_counter()
        if serialized:
            self.logger.debug("chain_start", name=serialized.get("name"))

    def on_chain_end(self, outputs, *, run_id=None, **kwargs):
        self.logger.info(
            "chain", source="callback", duration_ms=self._duration(run_id)
        )

    def on_chain_error(self, error, *, run_id=None, **kwargs):
        self.logger.error(
            "chain_error", source="callback", duration_ms=self._duration(run_id), error=str(error)
        )

    def _duration(self, run_id) -> float:
        started = self._started.pop(run_id, None)
        if started is None:
            return 0.0
        return round((time.perf_counter() - started) * 1000, 2)


def log_llm_call(logger, *, node: str, response, duration_ms: float) -> None:
    """Structured metrics for our provider seam (which is not a LangChain Runnable)."""
    logger.info(
        "llm_call",
        node=node,
        model=getattr(response, "model", ""),
        prompt_tokens=getattr(response, "prompt_tokens", 0),
        completion_tokens=getattr(response, "completion_tokens", 0),
        cached=bool(getattr(response, "cached", False)),
        duration_ms=round(duration_ms, 2),
    )
