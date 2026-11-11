"""Hosted LLM providers.

SDK imports are lazy so the graph, tests and offline demo run without the optional
dependencies installed.
"""
from __future__ import annotations

from blackbox.llm.base import LLMResponse


class OpenAIProvider:
    def __init__(
        self,
        *,
        api_key: str,
        default_model: str = "gpt-4o-mini",
        timeout_s: float = 30.0,
        max_retries: int = 2,
    ) -> None:
        from openai import AsyncOpenAI, OpenAI

        self.name = "openai"
        self.default_model = default_model
        self._client = OpenAI(
            api_key=api_key, timeout=timeout_s, max_retries=max_retries
        )
        # The async client is what the request path uses: a hosted round trip must not
        # hold a worker thread while it waits.
        self._async_client = AsyncOpenAI(
            api_key=api_key, timeout=timeout_s, max_retries=max_retries
        )

    def _request(
        self,
        *,
        system: str,
        prompt: str,
        model: str | None,
        temperature: float,
        max_tokens: int,
        json_mode: bool,
    ) -> dict:
        chosen = model or self.default_model
        request: dict = {
            "model": chosen,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            request["response_format"] = {"type": "json_object"}
        return request

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
        chosen = model or self.default_model
        request = self._request(
            system=system,
            prompt=prompt,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=json_mode,
        )
        response = self._client.chat.completions.create(**request)
        usage = response.usage
        return LLMResponse(
            text=response.choices[0].message.content or "",
            model=chosen,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
        )

    async def acomplete(
        self,
        *,
        system: str,
        prompt: str,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 512,
        json_mode: bool = False,
    ) -> LLMResponse:
        chosen = model or self.default_model
        request = self._request(
            system=system,
            prompt=prompt,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=json_mode,
        )
        response = await self._async_client.chat.completions.create(**request)
        usage = response.usage
        return LLMResponse(
            text=response.choices[0].message.content or "",
            model=chosen,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
        )


class AnthropicProvider:
    def __init__(
        self,
        *,
        api_key: str,
        default_model: str = "claude-3-5-haiku-20241022",
        timeout_s: float = 30.0,
        max_retries: int = 2,
    ) -> None:
        from anthropic import Anthropic, AsyncAnthropic

        self.name = "anthropic"
        self.default_model = default_model
        self._client = Anthropic(
            api_key=api_key, timeout=timeout_s, max_retries=max_retries
        )
        # The async client is what the request path uses: a hosted round trip must not
        # hold a worker thread while it waits.
        self._async_client = AsyncAnthropic(
            api_key=api_key, timeout=timeout_s, max_retries=max_retries
        )

    def _system(self, system: str, json_mode: bool) -> str:
        if json_mode:
            return f"{system}\nReply with a single valid JSON object and nothing else."
        return system

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
        chosen = model or self.default_model
        response = self._client.messages.create(
            model=chosen,
            system=self._system(system, json_mode),
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        text = "".join(block.text for block in response.content if block.type == "text")
        usage = response.usage
        return LLMResponse(
            text=text,
            model=chosen,
            prompt_tokens=getattr(usage, "input_tokens", 0) or 0,
            completion_tokens=getattr(usage, "output_tokens", 0) or 0,
        )

    async def acomplete(
        self,
        *,
        system: str,
        prompt: str,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 512,
        json_mode: bool = False,
    ) -> LLMResponse:
        chosen = model or self.default_model
        response = await self._async_client.messages.create(
            model=chosen,
            system=self._system(system, json_mode),
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        text = "".join(block.text for block in response.content if block.type == "text")
        usage = response.usage
        return LLMResponse(
            text=text,
            model=chosen,
            prompt_tokens=getattr(usage, "input_tokens", 0) or 0,
            completion_tokens=getattr(usage, "output_tokens", 0) or 0,
        )
