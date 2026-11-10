from blackbox.memory import EntityMemory
from blackbox.memory.updates import extract_hypotheses, update_entity_memory


def test_ruled_out_then_confirmed_updates_one_hypothesis():
    entity = EntityMemory()
    update_entity_memory(
        entity, user_message="We ruled out the 01:15 deploy as the cause."
    )
    assert len(entity.hypotheses) == 1
    assert entity.hypotheses[0].status == "ruled_out"

    update_entity_memory(
        entity, user_message="Actually, the 01:15 deploy was the cause."
    )
    assert len(entity.hypotheses) == 1
    assert entity.hypotheses[0].status == "confirmed"
    assert entity.hypotheses[0].history[0]["from"] == "ruled_out"


def test_ruled_out_with_reason_and_confirmed_without_the_match():
    entity = EntityMemory()
    update_entity_memory(
        entity,
        user_message="We ruled out the deploy because there was no code diff.",
    )
    update_entity_memory(entity, user_message="Actually the deploy was the cause.")

    assert len(entity.hypotheses) == 1
    assert entity.hypotheses[0].status == "confirmed"


def test_services_error_codes_timeline_and_actions_are_recorded():
    entity = EntityMemory()
    events = update_entity_memory(
        entity,
        user_message=(
            "At 01:15 we checked checkout-service; it reports ERR_POOL_EXHAUSTED. "
            "We restarted payment-gateway."
        ),
    )

    assert "checkout-service" in entity.services
    assert "payment-gateway" in entity.services
    assert "ERR_POOL_EXHAUSTED" in entity.error_codes
    assert "01:15" in entity.timeline_anchors
    assert any(action.action.startswith("restarted") for action in entity.actions)
    assert any(event.startswith("service:") for event in events)


def test_leading_article_does_not_split_a_hypothesis():
    entity = EntityMemory()
    update_entity_memory(entity, user_message="The deploy was ruled out.")
    update_entity_memory(entity, user_message="Actually the deploy was the cause.")

    assert len(entity.hypotheses) == 1
    assert entity.hypotheses[0].status == "confirmed"


def test_non_service_hyphenated_tokens_are_ignored():
    entity = EntityMemory()
    update_entity_memory(
        entity, user_message="The code-review found a read-only cache issue."
    )
    assert entity.services == []


def test_extract_hypotheses_handles_confirmation_phrasing():
    statements = extract_hypotheses(
        "The root cause was DB pool exhaustion. Pool exhaustion is confirmed."
    )
    assert any(status == "confirmed" for _, status in statements)


def test_updates_are_idempotent():
    entity = EntityMemory()
    message = "We ruled out the deploy. checkout-service ERR_TIMEOUT at 01:15."
    update_entity_memory(entity, user_message=message)
    first = entity.to_dict()
    events = update_entity_memory(entity, user_message=message)
    assert entity.to_dict() == first
    assert not [event for event in events if event.startswith("service:")]
