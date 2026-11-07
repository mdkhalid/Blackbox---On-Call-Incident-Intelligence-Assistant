# Chunking Strategy — Blackbox

See `docs/document-parsing-chunking.md` for full details. Summary below.

## Per Document Type

### Postmortems (30) — sections atomic
- Summary, Impact: 1 chunk each.
- Timeline: whole if ≤600 tokens else split by event.
- **Root Cause: atomic, never split mid-section** (max 800 tokens; split by paragraph only if oversized).
- Contributing Factors, Remediation, Action Items: 1 chunk each.
- Target 300–800 tokens/chunk.

### Runbooks (15)
- Symptom, Diagnosis, Mitigation, Escalation → 1 chunk each (merge short ones). Target 200–600 tokens.

### Architecture docs (10)
- Service description, Dependencies, Data flows, Known limits → 1 chunk each; dependencies split per dep if long. Target 300–700 tokens.

### Alert definitions (5)
- Single chunk per alert. Target 100–300 tokens.

## Justification
- Section boundaries align with semantic units; splitting mid-section destroys meaning (e.g., a Root Cause fragment is uninterpretable).
- Sizes chosen to fit embedding model context and keep retrieval precision high; verified during eval.
- Metadata (doc_type, services, severity, date, section, error_codes) on every chunk enables filtered retrieval.

## Idempotency
Deterministic chunk IDs (hash doc_id + section + part); upsert; re-ingest removes stale chunks by doc_id.
