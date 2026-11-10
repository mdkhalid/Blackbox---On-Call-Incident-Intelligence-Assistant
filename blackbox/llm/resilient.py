"""Resilience wrapper around any LLM provider.

The hosted SDKs already apply timeouts/retries, but the application still needs a bounded
concurrency limit, a circuit breaker and jittered retries that are visible to the graph.
"""
from __future__ import annotations

import random
import threading
import time

from blackbox.llm.base import LLMResponse


class LLMUnavailable(RuntimeError):
    pass


class CircuitBreaker:
    def __init__(self, *, threshold: int = 5, reset_s: float = 30.0) -> None:
        self.threshold = max(1, threshold)
        self.reset_s = max(0.0, reset_s)
        self._failures = 0
        self._opened_at: float | None = None
        self._lock = threading.Lock()

    def allow(self) -> bool:
        with self._lock:
            if self._opened_at is None:
                return True
            if time.monotonic() - self._opened_at >= self.reset_s:
                # Half-open: allow one probe through.
                self._opened_at = None
                self._failures = 0
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self.threshold:
                self._opened_at = time.monotonic()


class ResilientLLMProvider:
    def __init__(
        self,
        provider,
        *,
        timeout_s: float = 30.0,
        max_retries: int = 2,
        backoff_s: float = 0.2,
        max_concurrency: int = 8,
        failure_threshold: int = 5,
        reset_s: float = 30.0,
    ) -> None:
        self.provider = provider
        self.timeout_s = timeout_s
        self.max_retries = max(0, max_retries)
        self.backoff_s = max(0.0, backoff_s)
        self.semaphore = threading.BoundedSemaphore(max(1, max_concurrency))
        self.breaker = CircuitBreaker(threshold=failure_threshold, reset_s=reset_s)

    @property
    def name(self) -> str:
        return getattr(self.provider, "name", type(self.provider).__name__)

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 512,
        json_mode: bool = False,
    ) -> LLMResponse:
        if not self.breaker.allow():
            raise LLMUnavailable("LLM circuit breaker is open")

        if not self.semaphore.acquire(timeout=self.timeout_s):
            raise LLMUnavailable("LLM concurrency limit reached")

        try:
            for attempt in range(self.max_retries + 1):
                try:
                    response = self.provider.complete(
                        system=system,
                        prompt=prompt,
                        model=model,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        json_mode=json_mode,
                    )
                    self.breaker.record_success()
                    return response
                except Exception:
                    if attempt >= self.max_retries:
                        self.breaker.record_failure()
                        raise
                    delay = self.backoff_s * (2**attempt)
                    time.sleep(delay + random.uniform(0, 0.05))
        finally:
            self.semaphore.release()

        raise LLMUnavailable("LLM call failed")  # pragma: no cover
