"""LLM provider construction.

`auto` follows the embedding provider's rule: use a hosted model when a key exists,
otherwise fall back to the deterministic offline provider so a fresh clone works.
"""
from __future__ import annotations

from blackbox.config.settings import Settings, get_settings
from blackbox.llm.base import LLMProvider, LLMResponse
from blackbox.llm.cached import CachedLLMProvider
from blackbox.llm.fake import FakeLLMProvider
from blackbox.llm.providers import AnthropicProvider, OpenAIProvider

__all__ = [
    "AnthropicProvider",
    "CachedLLMProvider",
    "FakeLLMProvider",
    "LLMProvider",
    "LLMResponse",
    "OpenAIProvider",
    "build_llm_provider",
]


def build_llm_provider(
    settings: Settings | None = None,
    *,
    provider: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
) -> LLMProvider:
    settings = settings or get_settings()
    explicit = provider is not None
    choice = (provider or settings.llm_provider or "auto").lower()

    if choice == "fake":
        return FakeLLMProvider()
    if choice == "openai":
        key = api_key or settings.openai_api_key
        if not key:
            return _missing_key_fallback(settings, explicit, "OPENAI_API_KEY")
        return OpenAIProvider(api_key=key, default_model=model or settings.grading_model)
    if choice == "anthropic":
        key = api_key or settings.anthropic_api_key
        if not key:
            return _missing_key_fallback(settings, explicit, "ANTHROPIC_API_KEY")
        return AnthropicProvider(api_key=key, default_model=model or settings.grading_model)
    if choice == "auto":
        if settings.llm_provider == "anthropic" and settings.anthropic_api_key:
            return AnthropicProvider(
                api_key=settings.anthropic_api_key,
                default_model=model or settings.grading_model,
            )
        if settings.openai_api_key:
            return OpenAIProvider(
                api_key=settings.openai_api_key,
                default_model=model or settings.grading_model,
            )
        if settings.anthropic_api_key:
            return AnthropicProvider(
                api_key=settings.anthropic_api_key,
                default_model=model or settings.grading_model,
            )
        return FakeLLMProvider()
    raise ValueError(f"unknown LLM provider: {provider}")


def _missing_key_fallback(
    settings: Settings, explicit: bool, key_name: str
) -> LLMProvider:
    """Dev convenience: a keyless clone still runs. Production and explicit asks fail loudly."""
    if explicit or settings.env == "prod":
        raise ValueError(f"{key_name} is required when LLM_PROVIDER is set to this provider")
    return FakeLLMProvider()
