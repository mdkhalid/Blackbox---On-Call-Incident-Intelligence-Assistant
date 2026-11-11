from blackbox.config.settings import BudgetConfig, MemoryConfig
from blackbox.memory import ConversationMemory, build_context, count_tokens


def _memory(turns: int = 6) -> ConversationMemory:
    memory = ConversationMemory(
        MemoryConfig(working_turns=6, summary_trigger_turns=8)
    )
    for i in range(turns):
        memory.add_turn(f"user turn {i} " + "detail " * 10, "assistant reply " * 10)
    memory.entity.note_services(["checkout-service", "payment-gateway"])
    memory.entity.upsert_hypothesis("pool exhaustion", status="confirmed", evidence="pm-001")
    memory.summary.text = "summary " * 30
    return memory


def _chunks(count: int = 5) -> list[dict]:
    return [
        {"chunk_id": f"c{i}", "text": "evidence " * 20, "score": 1.0 - i / 10}
        for i in range(count)
    ]


def test_context_buckets_respect_their_allocations():
    budget = BudgetConfig()
    bundle = build_context(
        system_prompt="system " * 10,
        memory=_memory(),
        chunks=_chunks(),
        question="what changed?",
        budget=budget,
    )

    buckets = bundle.report["buckets"]
    assert buckets["entity"]["allocated"] <= budget.entity_memory_tokens
    assert buckets["summary"]["allocated"] <= budget.summary_tokens
    assert buckets["recent_turns"]["allocated"] <= budget.recent_turns_tokens
    assert buckets["retrieved_chunks"]["allocated"] <= budget.retrieved_chunks_tokens
    assert bundle.report["total_tokens"] <= bundle.report["total_cap"]


def test_eviction_follows_the_documented_order():
    budget = BudgetConfig(
        system_prompt_tokens=100,
        entity_memory_tokens=100,
        summary_tokens=100,
        recent_turns_tokens=100,
        retrieved_chunks_tokens=100,
        output_headroom_tokens=0,
        total_cap_tokens=10,
    )
    bundle = build_context(
        system_prompt="sys",
        memory=_memory(),
        chunks=_chunks(),
        question="q",
        budget=budget,
    )
    order = [event["bucket"] for event in bundle.report["evictions"]]
    assert order[0] == "recent_turns"
    assert order.index("recent_turns") < order.index("retrieved_chunks")
    assert order.index("retrieved_chunks") < order.index("summary")
    assert order.index("summary") < order.index("entity")


def test_system_prompt_is_never_evicted():
    system_prompt = "never drop this system prompt"
    budget = BudgetConfig(total_cap_tokens=5, output_headroom_tokens=0)
    bundle = build_context(
        system_prompt=system_prompt,
        memory=_memory(),
        chunks=_chunks(),
        question="q",
        budget=budget,
    )
    assert bundle.system_prompt == system_prompt
    assert all(event["bucket"] != "system" for event in bundle.report["evictions"])


def test_render_orders_entity_summary_and_recent_turns():
    bundle = build_context(
        system_prompt="sys",
        memory=_memory(),
        chunks=_chunks(),
        question="q",
        budget=BudgetConfig(),
    )
    rendered = bundle.render()
    assert rendered.index("ENTITY MEMORY") < rendered.index("INCIDENT SUMMARY")
    assert rendered.index("INCIDENT SUMMARY") < rendered.index("RECENT TURNS")


def test_report_exposes_requested_and_allocated_tokens():
    bundle = build_context(
        system_prompt="system " * 50,
        memory=_memory(),
        chunks=_chunks(),
        question="q",
        budget=BudgetConfig(system_prompt_tokens=10),
    )
    system = bundle.report["buckets"]["system"]
    assert system["requested"] > system["allocated"]
    assert system["allocated"] == 10
    assert count_tokens(bundle.system_prompt) == system["allocated"]


def test_dropped_chunks_are_the_lowest_scored():
    bundle = build_context(
        system_prompt="sys",
        memory=_memory(),
        chunks=_chunks(10),
        question="q",
        budget=BudgetConfig(retrieved_chunks_tokens=20),
    )
    kept = [chunk["chunk_id"] for chunk in bundle.chunks]
    assert "c0" in kept
    assert "c9" not in kept


def test_the_output_headroom_is_reserved_from_the_input_ceiling():
    """The ceiling applies to the input, because the headroom is the answer's room."""
    budget = BudgetConfig()
    bundle = build_context(
        system_prompt="system " * 10,
        memory=_memory(),
        chunks=_chunks(),
        question="what changed?",
        budget=budget,
    )
    report = bundle.report
    assert report["input_cap"] == budget.total_cap_tokens - budget.output_headroom_tokens
    assert report["total_tokens"] <= report["input_cap"]
    assert not report["over_cap"]


def test_the_chunk_room_is_reserved_when_the_chunks_are_not_retrieved_yet():
    """Memory is assembled before the graph retrieves.

    The chunk allocation has to be reserved up front, otherwise the evidence is added
    on top of an already-full context and the real prompt exceeds the cap.
    """
    budget = BudgetConfig()
    bundle = build_context(
        system_prompt="system " * 10,
        memory=_memory(),
        chunks=[],
        question="what changed?",
        budget=budget,
    )
    assert bundle.report["chunk_allowance"] >= budget.retrieved_chunks_tokens
    assert bundle.report["total_tokens"] <= bundle.report["input_cap"]


def test_supplied_chunks_are_counted_against_the_ceiling():
    """When the chunks are known they are part of the total, not an extra on top."""
    budget = BudgetConfig(retrieved_chunks_tokens=40)
    bundle = build_context(
        system_prompt="sys",
        memory=_memory(),
        chunks=_chunks(10),
        question="q",
        budget=budget,
    )
    assert bundle.report["total_tokens"] <= bundle.report["input_cap"]
    assert bundle.report["buckets"]["retrieved_chunks"]["allocated"] <= 40
