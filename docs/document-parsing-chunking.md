# Document Parsing & Chunking → Chroma — Blackbox

## Corpus
Minimum 60 docs, internally consistent (shared service names, error codes, dependencies):
- 30 Postmortems
- 15 Runbooks
- 10 Architecture docs
- 5 Alert definitions

## File Formats
- Source: Markdown (`.md`) with YAML frontmatter for metadata (title, type, service, severity, date). Easier to parse and version-control than PDF/DOCX.
- Loaders: `ingestion/loaders/{postmortem,runbook,arch,alert}_loader.py` parse frontmatter + body.

## Parsing Strategy
1. Read file → split frontmatter (YAML) and body.
2. Extract metadata: doc_type, title, services, severity, date, error_codes (regex from body).
3. Body split into sections by heading (H2/H3) per doc type.
4. Validate required sections present; log missing.

## Chunking Strategy (structure-aware)

### Postmortem (30 docs)
Sections: Summary, Impact, Timeline, Root Cause, Contributing Factors, Remediation, Action Items.
- **Root Cause**: atomic chunk (never split). If > max (default 800 tokens), split by paragraph, keep as separate chunks tagged `section=root_cause, part=i`.
- **Timeline**: chunk as whole if ≤ 600 tokens; else split by event (each bullet = chunk? or group). Keep chronological order in metadata.
- **Summary/Impact**: one chunk each.
- **Contributing Factors**: one chunk (or per factor if long).
- **Remediation/Action Items**: one chunk each.
- Chunk size target: 300–800 tokens.

### Runbook (15 docs)
Sections: Symptom, Diagnosis steps, Mitigation, Escalation.
- Each section = one chunk (Symptom+Diagnosis may merge if short).
- Target: 200–600 tokens.

### Architecture docs (10 docs)
Sections: Service description, Dependencies, Data flows, Known limits.
- Each section = one chunk; Dependencies section chunked per dependency if long.
- Target: 300–700 tokens.

### Alert definitions (5 docs)
- Single chunk per alert (small): name, condition, threshold, linked runbook.
- Target: 100–300 tokens.

## Metadata on Every Chunk
```json
{
  "chunk_id": "sha256(doc_id + section + part)",
  "doc_id": "pm-001",
  "doc_type": "postmortem",
  "title": "...",
  "section": "root_cause",
  "services": ["checkout-service", "payment-gateway"],
  "severity": "SEV1",
  "date": "2024-03-15",
  "error_codes": ["ERR_POOL_EXHAUSTED"],
  "tokens": 412
}
```

## Entity Extraction at Ingest
- Services, error codes, dependencies extracted via regex + LLM (structured) into Postgres tables:
  - `services(name, description)`
  - `error_codes(code, description)`
  - `dependencies(src_service, dst_service, type)`
  - `doc_entities(doc_id, entity_type, entity_value)`
- Enables graph lookups ("which services depend on payment gateway?") without embeddings.

## Idempotency
- Deterministic chunk IDs → Chroma `upsert`.
- On re-ingest of a doc: delete all chunks with that `doc_id`, then insert new. Or rely on upsert + remove stale (track chunk IDs per doc in Postgres).
- Pipeline tracks `ingested_docs(doc_id, content_hash, ingested_at)`; skip unchanged docs.

## Large Documents
For memory-safe ingestion of large files, see `docs/large-document-streaming.md`. Chunkers are implemented as generators; embedding/upsert happens in bounded batches.

## Chroma Storage
- Collection per embedding model + dimension (e.g. `blackbox_text-embedding-3-small`).
- Persist dir: `data/chroma/` (gitignored; rebuild via seed script).
- Metadata stored in Chroma for filtering; full metadata also in Postgres for joins.

## Token Counting
- Use tiktoken for chunk size measurement; ensure no chunk exceeds embedding model max (8191 for OpenAI).

## Validation
- After ingest: assert chunk count ≥ expected; sample chunks printed to log; eval retrieval sanity check.
- Document chunking strategy in `docs/chunking-strategy.md` (one of the four required).
