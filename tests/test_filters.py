import pytest

from blackbox.retrieval.filters import MetadataFilter, last_n_days, parse_filter


def test_empty_filter_compiles_to_none():
    assert MetadataFilter().to_chroma_where() is None
    assert MetadataFilter().is_empty()


def test_single_dimension_has_no_and_wrapper():
    where = MetadataFilter(doc_types=["postmortem"]).to_chroma_where()
    assert where == {"doc_type": "postmortem"}


def test_multiple_values_use_in_for_scalar_fields():
    where = MetadataFilter(severities=["SEV1", "SEV2"]).to_chroma_where()
    assert where == {"severity": {"$in": ["SEV1", "SEV2"]}}


def test_list_fields_use_contains_not_in():
    # `$in` compares whole metadata values; services is a list, so `$contains` is required.
    where = MetadataFilter(services=["checkout-service"]).to_chroma_where()
    assert where == {"services": {"$contains": "checkout-service"}}


def test_multiple_services_become_or_of_contains():
    where = MetadataFilter(services=["checkout-service", "auth-service"]).to_chroma_where()
    assert where == {
        "$or": [
            {"services": {"$contains": "checkout-service"}},
            {"services": {"$contains": "auth-service"}},
        ]
    }


def test_combined_filter_ands_dimensions():
    where = MetadataFilter(
        services=["checkout-service"], severities=["SEV1"], doc_types=["postmortem"]
    ).to_chroma_where()
    assert "$and" in where
    assert {"services": {"$contains": "checkout-service"}} in where["$and"]
    assert {"severity": "SEV1"} in where["$and"]
    assert {"doc_type": "postmortem"} in where["$and"]


def test_date_range_uses_numeric_ordinal_field():
    # Chroma range operators reject string operands, hence the date_ord shadow field.
    where = MetadataFilter(date_from="2024-01-01", date_to="2024-06-30").to_chroma_where()
    assert {"date_ord": {"$gte": 738886}} in where["$and"]
    assert {"date_ord": {"$lte": 739067}} in where["$and"]


def test_invalid_date_is_ignored_rather_than_crashing():
    where = MetadataFilter(date_from="not-a-date").to_chroma_where()
    assert where is None


def test_filter_values_are_cleaned_and_deduplicated():
    where = MetadataFilter(
        doc_types=[" postmortem ", "postmortem", "", "runbook"]
    ).to_chroma_where()
    assert where == {"doc_type": {"$in": ["postmortem", "runbook"]}}


def test_filter_values_are_capped():
    values = [f"svc-{i}" for i in range(100)]
    where = MetadataFilter(services=values).to_chroma_where()
    assert len(where["$or"]) == 25


def test_matches_mirrors_chroma_semantics():
    metadata = {
        "doc_type": "postmortem",
        "services": ["checkout-service"],
        "error_codes": ["ERR_POOL_EXHAUSTED"],
        "severity": "SEV1",
        "date_ord": 739014,
    }
    assert MetadataFilter(services=["checkout-service"]).matches(metadata)
    assert MetadataFilter(services=["auth-service"]).matches(metadata) is False
    assert MetadataFilter(severities=["SEV1"]).matches(metadata)
    assert MetadataFilter(severities=["SEV3"]).matches(metadata) is False
    assert MetadataFilter(date_from="2024-01-01").matches(metadata)
    assert MetadataFilter(date_from="2024-06-01").matches(metadata) is False
    assert MetadataFilter(error_codes=["ERR_AUTH_FAILED"]).matches(metadata) is False


def test_matches_handles_missing_date():
    metadata = {"doc_type": "alert", "severity": "SEV1"}
    assert MetadataFilter(date_from="2024-01-01").matches(metadata) is False
    assert MetadataFilter(doc_types=["alert"]).matches(metadata) is True


def test_parse_filter_accepts_loose_keys():
    f = parse_filter(
        {
            "doc_type": "postmortem",
            "service": "checkout-service",
            "errors": ["ERR_POOL_EXHAUSTED"],
            "severity": "SEV1",
        }
    )
    assert f.doc_types == ["postmortem"]
    assert f.services == ["checkout-service"]
    assert f.error_codes == ["ERR_POOL_EXHAUSTED"]
    assert f.severities == ["SEV1"]


def test_parse_filter_none_is_empty():
    assert parse_filter(None).is_empty()
    assert parse_filter({}).is_empty()


def test_last_n_days_window():
    import datetime as dt

    start, end = last_n_days(365, today=dt.date(2024, 6, 1))
    assert end == "2024-06-01"
    assert start == "2023-06-02"


@pytest.mark.parametrize("field", ["services", "error_codes"])
def test_cross_dimension_and(field):
    where = MetadataFilter(**{field: ["x"], "doc_types": ["postmortem"]}).to_chroma_where()
    assert len(where["$and"]) == 2
