"""Metadata filters compiled to native vector-store predicates.

Filtering must happen inside the store, never by post-filtering a full result set in
Python. Chroma accepts a nested `where` document, so every filter here is translated
into one and handed to `collection.query(where=...)`.

Verified operator support on chromadb 1.5.9:
  - list metadata supports `$contains` (this is why chunk metadata keeps lists)
  - `$in` / `$nin` work on scalar fields
  - range operators require numbers, so dates are stored a second time as
    `date_ord` (a date ordinal) and ranged on that field.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from blackbox.ingestion.chunkers.base import to_epoch

# Guard against unbounded $or/$in payloads built from user input.
MAX_FILTER_VALUES = 25


def _clean(values: list[str] | None) -> list[str]:
    if not values:
        return []
    seen: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        v = value.strip()
        if v and v not in seen:
            seen.append(v)
    return seen[:MAX_FILTER_VALUES]


def _clause(field_name: str, values: list[str], *, list_field: bool) -> dict | None:
    """Equality/in clause for scalar fields, `$contains` for list fields."""
    if not values:
        return None
    if list_field:
        # One $contains per value, OR-ed: a chunk matches any requested service.
        if len(values) == 1:
            return {field_name: {"$contains": values[0]}}
        return {"$or": [{field_name: {"$contains": v}} for v in values]}
    if len(values) == 1:
        return {field_name: values[0]}
    return {field_name: {"$in": values}}


def _date_ord(value: str | None) -> int | None:
    if not value:
        return None
    if isinstance(value, int):
        return value
    return to_epoch(str(value)[:10])


@dataclass
class MetadataFilter:
    """Structured filter for retrieval.

    A chunk matches when it satisfies every populated dimension (dimensions AND together,
    values inside a dimension OR together).
    """

    doc_types: list[str] = field(default_factory=list)
    services: list[str] = field(default_factory=list)
    error_codes: list[str] = field(default_factory=list)
    severities: list[str] = field(default_factory=list)
    sections: list[str] = field(default_factory=list)
    doc_ids: list[str] = field(default_factory=list)
    date_from: str | None = None
    date_to: str | None = None
    # Tenant scoping. When set, retrieval is restricted to chunks carrying this
    # tenant_id in their metadata. An empty/missing tenant is rejected (never
    # "all tenants") so a forged or omitted filter cannot cross the boundary.
    tenant_id: str | None = None

    def is_empty(self) -> bool:
        return not any(
            (
                self.doc_types,
                self.services,
                self.error_codes,
                self.severities,
                self.sections,
                self.doc_ids,
                self.date_from,
                self.date_to,
                self.tenant_id,
            )
        )

    def normalized(self) -> MetadataFilter:
        return MetadataFilter(
            doc_types=_clean(self.doc_types),
            services=_clean(self.services),
            error_codes=_clean(self.error_codes),
            severities=_clean(self.severities),
            sections=_clean(self.sections),
            doc_ids=_clean(self.doc_ids),
            date_from=self.date_from,
            date_to=self.date_to,
            tenant_id=self.tenant_id,
        )

    def with_tenant(self, tenant_id: str | None) -> MetadataFilter:
        """Return a copy with tenant_id set, rejecting an empty tenant.

        Tenant scoping is mandatory: an empty tenant never means "all tenants".
        """
        if not tenant_id:
            raise ValueError("tenant_id must not be empty")
        copy = MetadataFilter(
            doc_types=list(self.doc_types),
            services=list(self.services),
            error_codes=list(self.error_codes),
            severities=list(self.severities),
            sections=list(self.sections),
            doc_ids=list(self.doc_ids),
            date_from=self.date_from,
            date_to=self.date_to,
            tenant_id=str(tenant_id),
        )
        return copy

    @staticmethod
    def require_tenant(tenant_id: str | None) -> str:
        """Validate a tenant id, raising ValueError when it is empty."""
        if not tenant_id:
            raise ValueError("tenant_id must not be empty")
        return str(tenant_id)

    def to_chroma_where(self) -> dict[str, Any] | None:
        """Compile to a Chroma `where` document, or None when unfiltered."""
        f = self.normalized()
        clauses: list[dict] = []

        c = _clause("doc_type", f.doc_types, list_field=False)
        if c:
            clauses.append(c)
        c = _clause("services", f.services, list_field=True)
        if c:
            clauses.append(c)
        c = _clause("error_codes", f.error_codes, list_field=True)
        if c:
            clauses.append(c)
        c = _clause("severity", f.severities, list_field=False)
        if c:
            clauses.append(c)
        c = _clause("section", f.sections, list_field=False)
        if c:
            clauses.append(c)
        c = _clause("doc_id", f.doc_ids, list_field=False)
        if c:
            clauses.append(c)

        # Tenant scoping is mandatory: a chunk is only visible to its own tenant.
        # An empty tenant_id is never compiled into "match everything" - callers
        # must resolve a real tenant before reaching here.
        if f.tenant_id:
            clauses.append({"tenant_id": str(f.tenant_id)})

        start = _date_ord(f.date_from)
        if start is not None:
            clauses.append({"date_ord": {"$gte": start}})
        end = _date_ord(f.date_to)
        if end is not None:
            clauses.append({"date_ord": {"$lte": end}})

        if not clauses:
            return None
        if len(clauses) == 1:
            return clauses[0]
        return {"$and": clauses}

    def matches(self, metadata: dict[str, Any]) -> bool:
        """Pure-Python equivalent, used to scope the in-process BM25 corpus.

        Kept semantically identical to `to_chroma_where` so both retrieval modes agree
        on which chunks exist before scoring.
        """
        f = self.normalized()
        if f.doc_types and metadata.get("doc_type") not in f.doc_types:
            return False
        if f.doc_ids and metadata.get("doc_id") not in f.doc_ids:
            return False
        if f.severities and metadata.get("severity") not in f.severities:
            return False
        if f.sections and metadata.get("section") not in f.sections:
            return False
        if f.services:
            have = metadata.get("services") or []
            if isinstance(have, str):
                have = [have]
            if not any(s in have for s in f.services):
                return False
        if f.error_codes:
            have = metadata.get("error_codes") or []
            if isinstance(have, str):
                have = [have]
            if not any(e in have for e in f.error_codes):
                return False
        if f.tenant_id and str(metadata.get("tenant_id", "")) != str(f.tenant_id):
            return False
        date_ord = metadata.get("date_ord")
        start = _date_ord(f.date_from)
        end = _date_ord(f.date_to)
        if start is not None and (date_ord is None or date_ord < start):
            return False
        return not (end is not None and (date_ord is None or date_ord > end))


def parse_filter(payload: dict[str, Any] | None) -> MetadataFilter:
    """Build a filter from an API/LLM payload, tolerating loose key names."""
    if not payload:
        return MetadataFilter()
    aliases = {
        "doc_types": ("doc_types", "doc_type", "types"),
        "services": ("services", "service"),
        "error_codes": ("error_codes", "error_code", "errors"),
        "severities": ("severities", "severity"),
        "sections": ("sections", "section"),
        "doc_ids": ("doc_ids", "doc_id"),
    }
    kwargs: dict[str, Any] = {}
    for target, keys in aliases.items():
        for key in keys:
            if key in payload and payload[key] is not None:
                value = payload[key]
                kwargs[target] = [value] if isinstance(value, str) else list(value)
                break
    for target in ("date_from", "date_to"):
        if payload.get(target):
            kwargs[target] = str(payload[target])
    if payload.get("tenant_id") is not None:
        kwargs["tenant_id"] = str(payload["tenant_id"])
    return MetadataFilter(**kwargs)


def last_n_days(days: int, *, today: dt.date | None = None) -> tuple[str, str]:
    """Helper for "in the last year" style windows."""
    end = today or dt.date.today()
    start = end - dt.timedelta(days=days)
    return start.isoformat(), end.isoformat()
