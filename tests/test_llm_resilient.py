import pytest

from blackbox.llm.base import LLMResponse
from blackbox.llm.resilient import LLMUnavailable, ResilientLLMProvider


class FlakyProvider:
    name = "flaky"

    def __init__(self, failures: int):
        self.failures = failures
        self.calls = 0

    def complete(self, **kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("transient")
        return LLMResponse(text="ok", model="flaky")


def test_retries_transient_failures():
    provider = FlakyProvider(failures=2)
    resilient = ResilientLLMProvider(
        provider, max_retries=2, backoff_s=0, failure_threshold=5
    )
    response = resilient.complete(system="s", prompt="p")
    assert response.text == "ok"
    assert provider.calls == 3


def test_circuit_opens_after_threshold():
    provider = FlakyProvider(failures=99)
    resilient = ResilientLLMProvider(
        provider,
        max_retries=0,
        backoff_s=0,
        failure_threshold=1,
        reset_s=60,
    )
    with pytest.raises(RuntimeError):
        resilient.complete(system="s", prompt="p")
    with pytest.raises(LLMUnavailable):
        resilient.complete(system="s", prompt="p")
    assert provider.calls == 1


def test_provider_name_is_delegated():
    resilient = ResilientLLMProvider(FlakyProvider(failures=0))
    assert resilient.name == "flaky"
