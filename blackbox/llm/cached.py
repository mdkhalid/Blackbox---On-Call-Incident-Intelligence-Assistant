"""LLM response cache for development.

Keys include provider, model and every generation parameter, so a model or temperature
change can never return a stale answer. Disabled in production.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

from blackbox.cache.backend import CacheBackend
from blackbox.llm.base import LLMResponse


class CachedLLMProvider:
    def __init__(
        self,
        provider,
        cache: CacheBackend,
        *,
        ttl_s: float = 86400,
        prefix: str = "llm:",
    ) -> None:
        self.provider = provider
        self.cache = cache
        self.ttl_s = ttl_s
        self.prefix = prefix

    @property
    def name(self) -> str:
        return getattr(self.provider, "name", type(self.provider).__name__)

    def _key(
        self,
        *,
        system: str,
        prompt: str,
        model: str | None,
        temperature: float,
        max_tokens: int,
        json_mode: bool,
    ) -> str:
        payload = json.dumps(
            {
                "provider": self.name,
                "model": model,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "json_mode": json_mode,
                "system": system,
                "prompt": prompt,
            },
            sort_keys=True,
        )
        return f"{self.prefix}{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"

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
        key = self._key(
            system=system,
            prompt=prompt,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=json_mode,
        )
        cached = self.cache.get(key)
        if isinstance(cached, dict):
            data = dict(cached)
            data["cached"] = True
            return LLMResponse(**data)

        response = self.provider.complete(
            system=system,
            prompt=prompt,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=json_mode,
        )
        self.cache.set(key, asdict(response), self.ttl_s)
        return response
