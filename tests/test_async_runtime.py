"""The runtime must not occupy a worker thread for the duration of an LLM call.

With synchronous graph nodes, LangGraph offloads each node to the event loop's
thread pool and the thread stays occupied for as long as the provider call takes.
That caps the number of concurrent turns per pod at the pool size rather than at any
admission control, which is the single largest throughput ceiling in the system.

These tests pin the structural property that removes the ceiling: every graph node is
a coroutine, the provider has an async entry point, and the worker pool is bounded by
configuration rather than by the host's CPU count.
"""
import asyncio
import inspect
from concurrent.futures import ThreadPoolExecutor

from blackbox.config.settings import Settings
from blackbox.graph.nodes import (
    generate_node,
    grade_node,
    ground_check_node,
    honest_failure_node,
    retrieve_node,
    rewrite_node,
)
from blackbox.graph.router import direct_answer_node, entity_lookup_node, router_node
from blackbox.llm.base import LLMResponse
from blackbox.llm.fake import FakeLLMProvider
from blackbox.llm.resilient import ResilientLLMProvider
from blackbox.main import create_app

GRAPH_NODES = (
    router_node,
    entity_lookup_node,
    direct_answer_node,
    retrieve_node,
    grade_node,
    rewrite_node,
    generate_node,
    ground_check_node,
    honest_failure_node,
)


def test_every_graph_node_is_a_coroutine_function():
    """A synchronous node is offloaded to the thread pool and holds it while it runs."""
    for node in GRAPH_NODES:
        assert inspect.iscoroutinefunction(node), f"{node.__name__} is not async"


def test_the_providers_expose_an_async_entry_point():
    assert inspect.iscoroutinefunction(FakeLLMProvider.acomplete)
    assert inspect.iscoroutinefunction(ResilientLLMProvider.acomplete)


async def test_the_resilient_provider_awaits_its_provider():
    """The async path must await the provider, not call it in a worker thread."""
    calls: list[str] = []

    class RecordingProvider(FakeLLMProvider):
        async def acomplete(self, **kwargs):
            calls.append("async")
            return LLMResponse(text="ok", model="fake")

        def complete(self, **kwargs):
            calls.append("sync")
            return LLMResponse(text="ok", model="fake")

    provider = ResilientLLMProvider(RecordingProvider(), max_retries=0)
    response = await provider.acomplete(system="s", prompt="p")
    assert response.text == "ok"
    assert calls == ["async"]


async def test_the_resilient_provider_retries_asynchronously():
    attempts: list[int] = []

    class FlakyProvider(FakeLLMProvider):
        async def acomplete(self, **kwargs):
            attempts.append(1)
            if len(attempts) < 2:
                raise RuntimeError("transient")
            return LLMResponse(text="ok", model="fake")

    provider = ResilientLLMProvider(FlakyProvider(), max_retries=2, backoff_s=0.01)
    response = await provider.acomplete(system="s", prompt="p")
    assert response.text == "ok"
    assert len(attempts) == 2


def test_the_worker_pool_size_is_configurable():
    settings = Settings(max_worker_threads=7)
    assert settings.max_worker_threads == 7


async def test_the_lifespan_bounds_the_default_executor(settings):
    """The default executor is `min(32, cpu_count + 4)`; the app must set its own.

    The executor is an implementation detail of asyncio, so the test reads the same
    attribute the runtime uses.
    """
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        executor = asyncio.get_running_loop()._default_executor
        assert isinstance(executor, ThreadPoolExecutor)
        assert executor._max_workers == settings.max_worker_threads
