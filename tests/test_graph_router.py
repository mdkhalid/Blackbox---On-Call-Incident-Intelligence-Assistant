from blackbox.graph.router import classify_route, direct_answer_node, entity_lookup_node


def test_definition_question_routes_direct():
    decision = classify_route("What does p99 mean?")
    assert decision.route == "direct"
    answer = direct_answer_node({"query": "What does p99 mean?"})["answer"]
    assert "99th-percentile" in answer


def test_sla_question_about_a_service_still_retrieves():
    # "What is the SLA for X?" asks about a specific service, not the definition of SLA.
    decision = classify_route("What is the SLA for the lunar telemetry pipeline?")
    assert decision.route == "retrieval"


def test_greeting_routes_direct():
    assert classify_route("hello").route == "direct"


def test_capability_question_routes_direct():
    assert classify_route("what can you do?").route == "direct"


def test_incident_question_routes_retrieval():
    decision = classify_route("what caused ERR_POOL_EXHAUSTED in checkout-service?")
    assert decision.route == "retrieval"


def test_dependency_question_with_entity_store_routes_entity(entity_store):
    decision = classify_route(
        "which services depend on payment-gateway?", entity_store=entity_store
    )
    assert decision.route == "entity"


def test_dependency_question_without_store_falls_back_to_retrieval():
    decision = classify_route("which services depend on payment-gateway?")
    assert decision.route == "retrieval"


def test_unknown_service_dependency_falls_back_to_retrieval(entity_store):
    decision = classify_route(
        "which services depend on mystery-service?", entity_store=entity_store
    )
    assert decision.route == "retrieval"


def test_entity_lookup_builds_dependency_evidence(entity_store):
    out = entity_lookup_node(
        {"query": "which services depend on payment-gateway?"}, entity_store=entity_store
    )
    assert out["chunks"]
    assert out["chunks"][0]["chunk_id"].startswith("graph::")
    assert "checkout-service" in out["chunks"][0]["text"]


def test_entity_lookup_without_store_returns_empty():
    out = entity_lookup_node(
        {"query": "which services depend on payment-gateway?"}, entity_store=None
    )
    assert out["chunks"] == []


def test_direct_answer_is_grounded_in_capabilities():
    out = direct_answer_node({"query": "hello"})
    assert out["status"] == "direct"
    assert "incident" in out["answer"].lower()
