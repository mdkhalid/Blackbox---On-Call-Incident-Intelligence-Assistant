import pytest

from blackbox.api.store import (
    ConversationTenantMismatch,
    SqlAlchemyConversationRepository,
)


def _repo(tmp_path):
    repo = SqlAlchemyConversationRepository(f"sqlite:///{tmp_path / 'conversations.db'}")
    repo.init()
    return repo


def test_missing_conversation_returns_none(tmp_path):
    repo = _repo(tmp_path)
    assert repo.get("nope") is None
    assert repo.get_turns("nope") == []
    assert repo.last_turn("nope") is None


def test_conversation_state_round_trips(tmp_path):
    repo = _repo(tmp_path)
    record = repo.get_or_create("conv-1")
    record.memory.entity.incident_id = "INC-1"
    record.memory.entity.note_services(["checkout-service"])
    record.memory.entity.upsert_hypothesis("pool exhaustion", status="confirmed")
    record.memory.add_turn("p99 is spiking", "checking pool metrics")

    repo.save_state("conv-1", record.memory, {"total_tokens": 42})

    restored = repo.get("conv-1")
    assert restored is not None
    assert restored.memory.entity.incident_id == "INC-1"
    assert restored.memory.entity.services == ["checkout-service"]
    assert restored.memory.entity.hypotheses[0].status == "confirmed"
    assert restored.memory.working.turns[0].user == "p99 is spiking"
    assert restored.last_budget["total_tokens"] == 42


def test_turns_persist_with_trace_and_budget(tmp_path):
    repo = _repo(tmp_path)
    repo.get_or_create("conv-1")
    turn = repo.append_turn(
        "conv-1",
        "what happened?",
        "pool exhaustion",
        "answered",
        [{"node": "router", "route": "retrieval"}],
        {"total_tokens": 100},
        [{"chunk_id": "c1", "fused_score": 0.9, "rerank_score": None}],
    )

    assert turn.id is not None
    turns = repo.get_turns("conv-1")
    assert len(turns) == 1
    assert turns[0].user == "what happened?"
    assert turns[0].assistant == "pool exhaustion"
    assert turns[0].status == "answered"
    assert turns[0].trace[0]["node"] == "router"
    assert turns[0].budget["total_tokens"] == 100
    assert turns[0].chunks[0]["chunk_id"] == "c1"
    assert repo.last_turn("conv-1").assistant == "pool exhaustion"


def test_record_turn_persists_state_and_turn_atomically(tmp_path):
    repo = _repo(tmp_path)
    record = repo.get_or_create("conv-1")
    record.memory.add_turn("p99 spiking", "checking")

    turn = repo.record_turn(
        "conv-1",
        record.memory,
        {"total_tokens": 7},
        "p99 spiking",
        "checking",
        "answered",
        [{"node": "router"}],
        [{"chunk_id": "c1"}],
    )

    assert turn.id is not None
    restored = repo.get("conv-1")
    assert restored.memory.working.turns[0].user == "p99 spiking"
    assert restored.last_budget["total_tokens"] == 7
    assert repo.get_turns("conv-1")[0].chunks[0]["chunk_id"] == "c1"


def test_tenant_isolation_on_conversations_and_turns(tmp_path):
    repo = _repo(tmp_path)
    repo.get_or_create("c1", tenant_id="tenant-a")
    repo.append_turn(
        "c1", "u", "a", "answered", [], {}, tenant_id="tenant-a"
    )

    assert repo.get("c1", tenant_id="tenant-a") is not None
    assert repo.get("c1", tenant_id="tenant-b") is None
    assert repo.get_turns("c1", tenant_id="tenant-b") == []
    with pytest.raises(ConversationTenantMismatch):
        repo.get_or_create("c1", tenant_id="tenant-b")


def test_get_or_create_is_idempotent(tmp_path):
    repo = _repo(tmp_path)
    first = repo.get_or_create("conv-1")
    second = repo.get_or_create("conv-1")
    assert first.created_at == second.created_at
