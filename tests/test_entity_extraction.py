from blackbox.ingestion.entity_extraction import extract_entities


def test_extract_error_codes():
    text = "Failed with ERR_POOL_EXHAUSTED and ERR_TIMEOUT"
    e = extract_entities(text)
    assert "ERR_POOL_EXHAUSTED" in e.error_codes
    assert "ERR_TIMEOUT" in e.error_codes


def test_extract_services_known():
    text = "checkout-service and payment-gateway involved"
    e = extract_entities(text, known_services=["checkout-service", "payment-gateway", "inventory-service"])
    assert "checkout-service" in e.services
    assert "payment-gateway" in e.services
    assert "inventory-service" not in e.services


def test_extract_dependencies():
    text = "checkout-service depends on payment-gateway"
    e = extract_entities(text)
    assert ("checkout-service", "payment-gateway") in e.dependencies
