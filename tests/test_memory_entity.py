from blackbox.memory import EntityMemory


def test_upsert_hypothesis_updates_in_place():
    memory = EntityMemory()
    memory.upsert_hypothesis("DB pool exhaustion", status="open", evidence="p99 high")
    memory.upsert_hypothesis("db pool exhaustion.", status="confirmed", evidence="matches pm-001")

    assert len(memory.hypotheses) == 1
    hypothesis = memory.hypotheses[0]
    assert hypothesis.status == "confirmed"
    assert hypothesis.evidence == "matches pm-001"
    assert len(hypothesis.history) == 1
    assert hypothesis.history[0]["from"] == "open"
    assert hypothesis.history[0]["to"] == "confirmed"


def test_conflict_never_keeps_two_active_states():
    memory = EntityMemory()
    memory.upsert_hypothesis("deploy caused the regression", status="ruled_out", evidence="no diff")
    memory.upsert_hypothesis("deploy caused the regression", status="confirmed", evidence="rollback fixed it")

    active = [h for h in memory.hypotheses if h.status in ("open", "confirmed")]
    assert len(active) == 1
    assert active[0].status == "confirmed"


def test_services_actions_and_timeline_render():
    memory = EntityMemory(incident_id="INC-1")
    memory.note_services(["checkout-service", "payment-gateway"])
    memory.note_error_codes(["ERR_POOL_EXHAUSTED"])
    memory.add_action("checked pool metrics", outcome="saturated")
    memory.add_timeline_anchor("01:15 deploy")
    memory.upsert_hypothesis("pool exhaustion", status="confirmed", evidence="pm-001")

    rendered = memory.render()
    assert "INC-1" in rendered
    assert "checkout-service" in rendered
    assert "ERR_POOL_EXHAUSTED" in rendered
    assert "saturated" in rendered
    assert "01:15 deploy" in rendered
    assert "[confirmed] pool exhaustion" in rendered


def test_essentials_keeps_services_and_confirmed_hypotheses_only():
    memory = EntityMemory(incident_id="INC-1")
    memory.note_services(["checkout-service"])
    memory.add_action("checked logs", outcome="nothing")
    memory.add_timeline_anchor("01:15")
    memory.upsert_hypothesis("pool exhaustion", status="confirmed")
    memory.upsert_hypothesis("deploy regression", status="ruled_out")

    essentials = memory.essentials()
    assert essentials.services == ["checkout-service"]
    assert [h.text for h in essentials.hypotheses] == ["pool exhaustion"]
    assert essentials.actions == []
    assert essentials.timeline_anchors == []


def test_entity_memory_round_trips():
    memory = EntityMemory(conversation_id="conv-1", incident_id="INC-1")
    memory.note_services(["checkout-service"])
    memory.add_timeline_anchor("01:15 deploy")
    memory.upsert_hypothesis("pool exhaustion", status="confirmed", evidence="pm-001")

    restored = EntityMemory.from_dict(memory.to_dict())
    assert restored.to_dict() == memory.to_dict()


def test_unknown_hypothesis_status_is_rejected():
    import pytest

    with pytest.raises(ValueError):
        EntityMemory().upsert_hypothesis("x", status="maybe")
