from __future__ import annotations

import re

from blackbox.ingestion.models import ExtractedEntities

ERROR_CODE_RE = re.compile(r"\bERR_[A-Z_]+\b")
SERVICE_RE = re.compile(r"\b[a-z][a-z0-9]*(?:[-_][a-z0-9]+)+\b", re.IGNORECASE)
# The subject must itself look like a service name. Without that constraint, prose such as
# "handles core logic and depends on upstream services" yields the dependency
# ("and", "upstream"), which then answers real dependency questions incorrectly.
DEPENDENCY_RE = re.compile(
    r"(?P<src>[a-z][a-z0-9]*(?:[-_][a-z0-9]+)+)\s+"
    r"(?:depends\s+on|requires|uses)\s*:?\s*"
    r"(?P<targets>[^\n.]+)",
    re.IGNORECASE,
)


def extract_entities(text: str, known_services: list[str] | None = None) -> ExtractedEntities:
    services_found: set[str] = set()
    lower = text.lower()
    for s in (known_services or []):
        if s.lower() in lower:
            services_found.add(s)
    error_codes = sorted(set(ERROR_CODE_RE.findall(text)))
    known = {s.lower(): s for s in (known_services or [])}

    deps: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for match in DEPENDENCY_RE.finditer(text):
        src = match.group("src")
        if known and src.lower() not in known:
            continue
        src = known.get(src.lower(), src)
        for raw_target in SERVICE_RE.findall(match.group("targets")):
            if known and raw_target.lower() not in known:
                continue
            dst = known.get(raw_target.lower(), raw_target)
            dependency = (src, dst)
            if src.lower() == dst.lower() or dependency in seen:
                continue
            seen.add(dependency)
            deps.append(dependency)

    return ExtractedEntities(
        services=sorted(services_found),
        error_codes=error_codes,
        dependencies=deps,
    )
