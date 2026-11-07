from __future__ import annotations

import re

from blackbox.ingestion.models import ExtractedEntities

ERROR_CODE_RE = re.compile(r"\bERR_[A-Z_]+\b")
SERVICE_RE = re.compile(r"\b[a-z][a-z0-9]*(?:[-_][a-z0-9]+)+\b", re.IGNORECASE)
DEPENDENCY_RE = re.compile(r"(\w[\w-]*)\s+(?:depends on|requires|uses)\s+(\w[\w-]*)", re.IGNORECASE)


def extract_entities(text: str, known_services: list[str] | None = None) -> ExtractedEntities:
    services_found: set[str] = set()
    lower = text.lower()
    for s in (known_services or []):
        if s.lower() in lower:
            services_found.add(s)
    error_codes = sorted(set(ERROR_CODE_RE.findall(text)))
    deps: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for m in DEPENDENCY_RE.finditer(text):
        d = (m.group(1), m.group(2))
        if d not in seen:
            seen.add(d)
            deps.append(d)
    return ExtractedEntities(
        services=sorted(services_found),
        error_codes=error_codes,
        dependencies=deps,
    )
