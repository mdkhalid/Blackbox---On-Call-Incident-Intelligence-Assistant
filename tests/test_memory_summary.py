from blackbox.config.settings import MemoryConfig
from blackbox.llm import FakeLLMProvider
from blackbox.memory import ConversationMemory, count_tokens


def _memory(**overrides) -> ConversationMemory:
    values = {"working_turns": 2, "summary_trigger_turns": 3, **overrides}
    return ConversationMemory(MemoryConfig(**values))


def test_recompression_preserves_ruled_out_hypothesis():
    """The headline requirement: a ruled-out hypothesis must survive compression."""
    memory = _memory()
    memory.add_turn(
        "We ruled out the deploy as the cause because there was no code diff.",
        "Understood, deploy is ruled out.",
    )
    memory.add_turn("Checkout p99 is spiking.", "Looking at the pool metrics.")
    memory.add_turn("Any update?", "Still checking.")

    assert memory.compression_due
    assert memory.maybe_compress(FakeLLMProvider())
    assert "ruled out" in memory.summary.text.lower()


def test_no_compression_below_trigger():
    memory = _memory(summary_trigger_turns=4)
    memory.add_turn("one", "one")
    memory.add_turn("two", "two")

    assert not memory.compression_due
    assert memory.maybe_compress(FakeLLMProvider()) is False
    assert memory.summary.text == ""


def test_compression_keeps_only_recent_turns_verbatim():
    memory = _memory()
    for i in range(3):
        memory.add_turn(f"user {i}", f"assistant {i}")

    memory.maybe_compress(FakeLLMProvider())
    assert len(memory.working.turns) == memory.config.working_turns
    assert [turn.user for turn in memory.working.recent()] == ["user 1", "user 2"]


def test_compression_is_cumulative():
    memory = _memory(summary_trigger_turns=3, summary_max_tokens=500)
    memory.add_turn("unique-alpha was ruled out early.", "noted")
    memory.add_turn("second turn", "noted")
    memory.add_turn("third turn", "noted")
    memory.maybe_compress(FakeLLMProvider())
    assert "unique-alpha" in memory.summary.text

    memory.add_turn("fourth turn", "noted")
    memory.add_turn("fifth turn", "noted")
    memory.add_turn("sixth turn", "noted")
    memory.maybe_compress(FakeLLMProvider())
    assert "unique-alpha" in memory.summary.text
    assert memory.summary.compression_count == 2


def test_summary_respects_the_token_cap():
    memory = _memory(summary_max_tokens=8)
    for i in range(3):
        memory.add_turn(
            f"turn {i} ruled out hypothesis {i} with a long explanation", "noted"
        )
    memory.maybe_compress(FakeLLMProvider())
    assert count_tokens(memory.summary.text) <= 8


def test_turns_since_compression_resets():
    memory = _memory()
    for i in range(3):
        memory.add_turn(f"user {i}", f"assistant {i}")
    memory.maybe_compress(FakeLLMProvider())
    assert memory.summary.turns_since_compression == 0
