"""Provider request/response contracts, without touching a real API.

The providers import their SDK lazily, so these tests construct them with
`object.__new__` and inject a stub client. That pins the request shape (model,
messages, JSON mode) and the response mapping (usage fields) without a network call
or the optional SDKs installed.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from blackbox.llm.providers import AnthropicProvider, OpenAIProvider


class _OpenAIStub:
    def __init__(self) -> None:
        self.last: dict | None = None
        self.chat = SimpleNamespace(completions=self)
        self._async = False

    def create(self, **kwargs):
        if self._async:
            return _maybe_await(self, kwargs)
        self.last = kwargs
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=SimpleNamespace(prompt_tokens=11, completion_tokens=4),
        )


class _OpenAIAsyncStub(_OpenAIStub):
    def __init__(self) -> None:
        super().__init__()
        self._async = True
        self.chat = SimpleNamespace(completions=self)


async def _maybe_await(stub, kwargs):
    stub.last = kwargs
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="async ok"))],
        usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2),
    )


def _openai_provider(client, *, default_model="gpt-4o-mini"):
    provider = object.__new__(OpenAIProvider)
    provider.name = "openai"
    provider.default_model = default_model
    provider._client = client
    provider._async_client = client
    return provider


def test_openai_request_and_response_mapping():
    client = _OpenAIStub()
    provider = _openai_provider(client)

    response = provider.complete(
        system="sys", prompt="user", temperature=0.2, max_tokens=64, json_mode=True
    )

    assert client.last["model"] == "gpt-4o-mini"
    assert client.last["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "user"},
    ]
    assert client.last["response_format"] == {"type": "json_object"}
    assert response.text == "ok"
    assert response.prompt_tokens == 11
    assert response.completion_tokens == 4


def test_openai_async_completion():
    client = _OpenAIAsyncStub()
    provider = _openai_provider(client)

    response = asyncio.run(provider.acomplete(system="s", prompt="p", model="m"))

    assert response.text == "async ok"
    assert client.last["model"] == "m"


class _AnthropicStub:
    def __init__(self, *, is_async: bool = False) -> None:
        self.last: dict | None = None
        self._is_async = is_async
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        if self._is_async:
            return _anthropic_async(self, kwargs)
        self.last = kwargs
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="ok")],
            usage=SimpleNamespace(input_tokens=5, output_tokens=2),
        )


async def _anthropic_async(stub, kwargs):
    stub.last = kwargs
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text="async ok")],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )


def _anthropic_provider(client):
    provider = object.__new__(AnthropicProvider)
    provider.name = "anthropic"
    provider.default_model = "claude-3-5-haiku-20241022"
    provider._client = client
    provider._async_client = client
    return provider


def test_anthropic_json_mode_appends_instruction():
    client = _AnthropicStub()
    provider = _anthropic_provider(client)

    response = provider.complete(system="sys", prompt="p", json_mode=True)

    assert "single valid JSON object" in client.last["system"]
    assert client.last["messages"] == [{"role": "user", "content": "p"}]
    assert response.prompt_tokens == 5
    assert response.completion_tokens == 2


def test_anthropic_async_completion():
    client = _AnthropicStub(is_async=True)
    provider = _anthropic_provider(client)

    response = asyncio.run(provider.acomplete(system="s", prompt="p"))

    assert response.text == "async ok"
