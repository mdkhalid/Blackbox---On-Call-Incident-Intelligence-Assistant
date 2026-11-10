# Phase-Wise Implementation Plan — Blackbox

On-Call Incident Intelligence Assistant. Python stack. Total budget 25–32 hrs.

Status: Phase 0 ✅, Phase 1 ✅, Phase 2 ✅, Phase 3 ✅, Phase 4 ✅, Phase 5 ✅, Phase 6 ✅, Phase 7 ✅ done. Phase 8 ⬅ next.

Environment: project-local `.venv` at `.venv/`; all dependencies installed there, none globally. Driver is `psycopg[binary]` because SQLAlchemy 2.1 resolves `postgresql://` to psycopg 3. Tests: **254 passing**, `ruff` clean.

## Phase 0 — Setup & Scaffolding (2 hrs) ✅ DONE
- Repo layout, pyproject, config (env + YAML), structlog logging, request-id middleware, health endpoints.
- Dockerfile + docker-compose (app, postgres, redis). Tests: 15. ruff clean. Committed (ff7438f).

## Phase 1 — Corpus & Ingestion (6 hrs) ✅ DONE
- 60-doc deterministic corpus (30 PM, 15 RB, 10 arch, 5 alerts).
- Loaders, structure-aware chunkers (Root Cause atomic), metadata, entity extraction + SQLAlchemy store.
- Embedding providers (Fake/OpenAI/ST), Chroma wrapper (idempotent), ingestion pipeline (content-hash skip, batched).
- Tests: 23. ruff clean. Committed (ff7438f).

## Phase 2 — Retrieval (5 hrs) ✅ DONE
- Dense retriever (`retrieval/dense.py`): cosine similarity over Chroma; distance → similarity.
- Sparse retriever (`retrieval/sparse.py`): in-process BM25 with an identifier-preserving tokenizer.
- Fusion (`retrieval/fusion.py`): RRF + normalized weighted sum, both config-driven.
- Reranker (`retrieval/rerank.py`): lexical (offline), cross-encoder, LLM, or null — measured.
- Custom LangChain retriever (`retrieval/hybrid_retriever.py`): `BaseRetriever`, composes with LCEL.
- Metadata filtering compiled to native Chroma predicates (`retrieval/filters.py`), never app-side.
- Retrieval timeout wrapper (`retrieval/timeout.py`); cold-start work excluded from the budget.
- Offline embedding provider (`OnnxEmbeddingProvider`) so retrieval + eval run with no API key.
- Tests: 164 total, ruff clean.
- **Pulled forward from Phase 7** (it gates every later decision, so it could not wait):
  - `eval/dataset.json` — 35 questions, labels derived from corpus frontmatter.
  - `eval/metrics.py` — MRR / recall@k / precision@k, document-level dedup.
  - `eval/retrieval_eval.py` — the 4-strategy comparison + configurable fusion sweep.
  - `scripts/build_eval_dataset.py` — regenerates the question set from the corpus.
  - `docs/retrieval-results.md` — generated, no hand-written numbers.

### Measured outcome (k=5, seeded corpus)
| Strategy | MRR | R@5 | P@5 |
|---|---|---|---|
| dense-only | 0.8548 | 0.8581 | 0.4433 |
| sparse-only (BM25) | 0.8571 | 0.8510 | 0.3510 |
| hybrid (RRF) | **0.9214** | **0.9067** | **0.4548** |
| hybrid + rerank | 0.8667 | 0.9219 | 0.4543 |

### Decisions the numbers forced (not the defaults)
- **Fusion weights 0.75/0.25, from a 21-point sweep.** 0.75–0.80 is a flat plateau at MRR
  0.9214; 0.75 picked mid-plateau, not at an arbitrary argmax. Justified, not intuited.
- **Reranking disabled by default.** It costs 0.055 MRR here while gaining 0.015 recall:
  it lifts single-doc `structural` questions but drops `exact_token` ones, because runbooks
  state the service+code pair more compactly than the postmortem does. Kept implemented and
  selectable per request so it can be re-measured on a real corpus.
- **Dense beats BM25 on exact-token queries (0.90 vs 0.85)** — contrary to the assignment's
  premise. Cause is corpus shape (short, templated docs give identifiers a tight, repeated
  context). Documented as measured rather than re-labelled to match expectations. Hybrid
  still lifts exact-token MRR to 1.00, which is the outcome that actually matters.
- `paraphrase` is where the retrievers fail in opposite directions (dense 1.00 vs sparse
  0.30) — the genuine justification for fusing rather than picking one.

### Bugs found and fixed while building this
- Splitting error codes into sub-tokens made `ERR_AUTH_FAILED` retrieve `ERR_AUTH_TIMEOUT`;
  underscored identifiers are now kept whole.
- The lexical reranker scrambled good fused rankings on stopword-heavy queries; it now
  abstains when a query carries no content term.
- Cold-start work (BM25 index build, ONNX model load) was charged to the first request's
  timeout budget and aborted healthy queries; warm-up now precedes the deadline.
- Chunk metadata emitted comma-joined strings, making `$contains` filtering impossible;
  lists now stay lists and dates carry a numeric `date_ord` shadow field.
- Lexical reranker needed corpus IDF, so `HybridRetriever.invalidate()` now rebuilds both
  the BM25 index and the reranker together.

### Post-sign-off defects found during Phase 3 smoke testing
Two ingestion bugs were hidden by the unit fixtures and only appeared against the real
60-document corpus. Both are fixed with regression tests.
- **Rebuilt Chroma + surviving `entities.db` skipped the whole corpus.** Idempotency
  checked only the content hash in the ingest ledger, so an empty vector store was treated
  as "already ingested". The skip now also requires the document's chunks to be present
  (`ChromaVectorStore.has_doc`), which is the documented Chroma-recovery path.
- **Dependency extraction missed the corpus's own syntax.** The seeded architecture docs
  say `checkout-service depends on: payment-gateway, inventory-service.`; the old regex
  matched only `A depends on B`, and on the prose sentence it extracted the bogus edge
  `and -> upstream`. The graph's entity route was therefore answering
  "no services depend on payment-gateway" when two do. The extractor now accepts colon +
  comma lists, requires a service-shaped subject, and filters against the document's
  declared services.
- `config.yaml` carried a stale rerank comment (0.9095 → 0.8857) from an earlier run; the
  regenerated results are 0.9214 → 0.8667.

## Phase 3 — LangGraph Pipeline (5 hrs) ✅ DONE
- State (`graph/state.py`): JSON-serialisable typed state carrying the query, filters,
  chunks with their full score trail, grade history, rewrite count, claims, ground-check
  verdicts, status, token usage, timings and the trace buffer.
- Nodes (`graph/nodes.py`, `graph/router.py`): `router`, `entity_lookup`, `direct_answer`,
  `retrieve`, `grade`, `rewrite`, `generate`, `ground_check`, `honest_failure`.
- Graph (`graph/build.py`): conditional routing, bounded rewrite cycle, event-level async
  streaming (`stream_graph_events`) and a sync `run_graph` helper.
- LLM layer (`blackbox/llm/`): narrow `complete()` protocol, lazy OpenAI/Anthropic
  providers, and a deterministic offline provider that performs lexical grading, extractive
  generation and identifier-preserving rewriting. The graph runs with no API key.
- Router: deterministic. Entity/graph only for dependency questions naming a known
  service; direct only for greetings/capability questions; everything else retrieves.
- Grading: cheap-model JSON verdict; malformed or missing output defaults to *insufficient*.
- Ground check: deterministic citation tracing, not an LLM call. Each claim's terms must
  appear in its cited chunk (or the best retrieved chunk); unsupported claims are stripped,
  and an answer with zero supported claims routes to honest failure.
- Honest failure: states what was searched, the filters, the number of rewrites and why it
  stopped. Never fabricates precedent.
- Guardrails wired: per-turn LLM call cap, secret scrubbing on terminal answers, retrieval
  failures/timeouts degrade into an honest answer rather than a 500.
- Tests: 205 total (+41 across the phase and the ingestion regressions), ruff clean.

### Verified offline against the seeded corpus (no API key)
- 60 documents → 315 chunks; dependency graph reconstructed correctly (7 edges).
- "What caused ERR_POOL_EXHAUSTED in checkout-service?" → retrieval → answered with 1/1
  claims grounded and the retrieval score trail in the trace.
- "Which services depend on payment-gateway?" → entity → "checkout-service, order-service".
- "What is the capital of France?" → 2 rewrites → honest failure naming the query.
- "hello" → direct, zero LLM calls.

### Decisions
- **Ground check is deterministic.** "Does this claim trace to a chunk" is a structural
  property, so an LLM judge would add cost and a new failure mode without adding evidence.
- **Router defaults to retrieval.** A misrouted incident question loses retrieval entirely;
  entity/direct paths are taken only on unambiguous query shapes.
- **Direct answers are restricted to meta questions** and are templated, so they cannot
  hallucinate incident facts. Incident questions always retrieve.
- **Streaming was designed in.** `stream_graph_events` yields per-node updates plus a final
  merged state; Phase 5 SSE and Phase 6's viewer consume the same events.
- **The trace is the state.** Every node records its timing and decisions in one append-only
  list, so there is no separate instrumentation path that can drift from execution.

## Phase 4 — Memory (4 hrs) ✅ DONE
- `memory/models.py`: `Turn`, `Hypothesis`, `Action` and `EntityMemory` with in-place
  conflict resolution. A contradicted hypothesis updates its status and keeps an audit
  entry; two active states never coexist.
- `memory/working.py`: last N exchanges verbatim (default 6), oldest-first overflow.
- `memory/summary.py` + `memory/prompts.py`: cumulative, incident-aware compression with an
  explicit preservation contract (ruled-out hypotheses + reason, confirmed facts, actions
  and outcomes, timeline anchors, identifiers) and a token-capped output.
- `memory/conversation.py`: trigger policy (`summary_trigger_turns`, default 8) and
  cumulative compression; JSON round-trip for Phase 5 persistence.
- `memory/context.py`: budgeted assembly in injection order entity → summary → recent
  turns → chunks → question. Eviction order: oldest recent turns → lowest-score chunks →
  summary compression → entity essentials; the system prompt is never evicted. Emits the
  report `/budget` will serve.
- `memory/tokens.py`: tiktoken counting/truncation with an offline fallback.
- Graph: `initial_state(context=..., budget=...)`; `generate` injects the assembled memory
  context as its own section, separate from retrieved evidence and from history.
- Tests: 229 total (+24), ruff clean.

### Verified offline against the seeded corpus
- Four-turn incident thread compresses to a summary that still contains "deploy ruled out";
  entity memory renders confirmed pool exhaustion beside the ruled-out deploy, plus the
  timeline anchor and error code.
- Real corpus turn: 616/6024 tokens, no evictions; the generate prompt contains both
  `ENTITY MEMORY` and `INCIDENT SUMMARY`; the graph still answers with grounded evidence.

### Decisions
- **Entity memory is a conversation-state field**, not its own store. Phase 5 persists the
  whole state; keeping the graph pure avoided a second source of truth.
- **The preservation list is a prompt contract backed by a test**, because "incident-aware"
  is only credible if a ruled-out hypothesis provably survives compression.
- **Enforcement and reporting share one token counter**, so `/budget` cannot disagree with
  the limit that was actually enforced.
- **Context is injected separately from history and evidence**, so provenance stays visible
  in the trace and retrieved chunks are never duplicated into the memory block.

## Phase 5 — API + Streaming (3 hrs) ✅ DONE
- `api/store.py`: SQLAlchemy conversation repository — memory JSON, last budget, and every
  turn's trace and budget. One schema runs on Postgres in prod and SQLite in tests.
- `api/services.py`: lazy runtime (retriever, LLM, graph, entity store) and lazy storage so
  health checks are free. Dev falls back to SQLite; prod fails fast with a clear 503.
- `api/routes/chat.py`: `POST /api/chat` SSE — node events, token events emitted only after
  ground check, then a final event carrying the trace, chunk scores, grade, ground check and
  budget. The turn is persisted before the final event. 400 (empty/invalid id), 413
  (oversized), 409 (concurrent turn), 503 (runtime/storage unavailable).
- `api/routes/conversations.py`: conversation summary, `/trace`, `/memory`, `/budget`.
- `api/routes/operations.py`: `POST /api/ingest` (workspace-confined, optional force,
  invalidates BM25 + reranker) and `POST /api/eval/run` (serialised, metrics JSON, optional
  fusion sweep; does not rewrite docs).
- Budget wiring: graph evidence fitting now uses the same tiktoken counter as memory, and
  `/budget` merges post-retrieval chunk tokens into the pre-turn allocation.
- Dockerfile ships `seed/` and `scripts/` so `/api/ingest` works in the container.
- Tests: 246 total (+17), ruff clean.

### Verified offline against the seeded corpus
- Real `TestClient` run: chain `router → retrieve → grade → generate → ground_check`, token
  bytes reconstitute the final answer exactly, and the turn/trace/memory/budget are readable
  afterwards (703/6024 tokens; five buckets including retrieved chunks).
- A second turn on the same conversation persisted as turn 2; unknown conversations return
  404; `/api/ingest` and `/api/eval/run` are covered by tests with the offline embedder.
- SSE node events expose the path as it executes; no answer text is emitted before ground
  check passes.

### Decisions
- **Token events are post-ground-check chunks, not raw decode tokens.** Shipping decode
  tokens would expose unverified claims before the check that removes them. Transport
  streaming is preserved; hosted-provider decode streaming is a future improvement.
- **One active turn per conversation** is enforced with a per-conversation asyncio lock
  (409). This is a single-process v1 guarantee; multi-instance needs a Postgres advisory
  lock, noted for Phase 8.
- **Dev SQLite fallback, prod hard failure.** The DB stays the source of truth; the fallback
  exists so a keyless/local demo runs without Postgres.
- **`/api/ingest` is workspace-confined and lock-serialised.** Path traversal is rejected and
  a second concurrent ingest gets 409.
- **The eval endpoint reads metrics but never writes `docs/`.** Results stay reproducible via
  the documented script; the API serves the same numbers.

## Phase 6 — Trace Viewer UI (2 hrs) ✅ DONE
- `api/static/index.html`: dependency-free viewer served at `/` and `/ui` by FastAPI.
- Replay: conversation id → `/trace` + `/memory`; per turn renders the graph path, chunks
  with dense/sparse/fused/rerank scores and ranks, grading attempts and reasons, rewrites,
  ground-check counts, and the token budget table with evictions.
- Live: `POST /api/chat` via `fetch` + `ReadableStream` with incremental SSE parsing; node
  chips and answer text appear as the turn runs, then the persisted trace reloads.
- Turn records now persist chunk score trails (`chunks_json`, with a forward migration) so
  replay shows the same retrieval evidence as the live final event.
- Tests: 249 total (+3), ruff clean.

### Decisions
- **One static file, no build step.** The viewer is served by the same FastAPI process, so
  the demo is a single command and the Docker image needs no Node toolchain.
- **DOM built with `textContent`, never `innerHTML` from model or corpus data.** Messages,
  answers and entity values are untrusted text; the viewer must not become an XSS surface.
- **Replay is the default; live proves streaming.** Both consume the same trace payload the
  API persists, so the UI needs no second API.
- **Chunk text is persisted but not rendered.** The viewer is about provenance and scores;
  showing raw chunk text would duplicate the corpus in the UI.

### Verified
- HTTP tests confirm `/` and `/ui` serve the viewer, that it references the trace endpoints,
  and that health still works alongside it. UI behaviour is exercised through the same API
  contract; browser screenshot automation was skipped because `agent-browser` is not
  installed in this environment.

## Phase 7 — Eval Harness ✅ DONE
- ✅ `eval/dataset.json`: 35 questions with corpus-derived source docs (Phase 2).
- ✅ `retrieval_eval.py`: MRR/P@k/R@k across dense/sparse/hybrid/hybrid+rerank (Phase 2).
- ✅ Results written to `docs/retrieval-results.md`; reproduction command in README.
- ✅ Retrieval metrics exposed as JSON through `POST /api/eval/run` (Phase 5).
- ✅ `groundedness_eval.py`: draft claim support, answer rate, abstention accuracy and a
  5-question unanswerable set; generated `docs/groundedness-results.md`.
- ✅ `POST /api/eval/run` accepts `groundedness:true` (and `limit`) and returns the same
  summary under the existing eval lock.
- ✅ Final results refreshed from real runs; no hand-written numbers.

### Measured outcome (offline provider, seeded corpus)
| Metric | Value |
|---|---|
| Answer rate (35 answerable) | 0.8857 (31/35) |
| Abstention accuracy (5 unanswerable) | 1.0000 (5/5) |
| False-answer rate (unanswerable) | 0.0000 |
| Draft claim support | 1.0000 (31/31) |
| Unsupported claims stripped | 0 |

- All four answer failures are `paraphrase` questions, consistent with the retrieval
  table's finding on this templated corpus — they abstain rather than invent precedent.
- Claim support is computed on drafted claims before stripping. Reporting the shipped
  claims would always be 1.0 by construction and would measure nothing.
- The offline provider is extractive, so its 1.0 is a floor. The same harness and checker
  produce the generator's real overreach with a hosted model.

### Bug found while measuring
- The offline generator took the *first sentence of the first* evidence chunk (often a
  runbook's "1." step), which looked like a hallucination and caused false honest failures.
  It now selects the best-matching evidence sentence. That moved the offline answer rate
  from 0.7429 to 0.8857 and draft support from 0.8387 to 1.0000 by removing the artifact,
  not by loosening the checker.

## Phase 8 — Observability, Caching, Hardening ⬜ TODO
- ⬜ LangChain callbacks: latency + token counts per LLM call, retrieval and chain step.
  (The graph records per-node timings and token usage today, but not via callbacks.)
- ⬜ LLM response caching for development (identical prompts).
- ◐ Structured JSON logging with request id — done in Phase 0; add graph-level context.
- ◐ Config validation — pydantic validators exist for retrieval/graph/budget; add
  cross-field checks and prod-only requirements.
- ⬜ Redis cache integration for retrieval/embedding caches, invalidated on ingest.
- ⬜ Rate limiting and CORS allowlist from `docs/guardrails.md` / `api-security.md`.
- ⬜ Prompt-injection detection (log/flag in dev, optional block in strict mode).
- ⬜ Postgres advisory lock for per-conversation turns when running more than one instance.

## Phase 9 — Docs & Submission ⬜ TODO
- ◐ README — covers setup, architecture, both results tables, API, memory and viewer;
  final polish remains.
- ✅ `docs/chunking-strategy.md`, `docs/memory-design.md`, `docs/retrieval-results.md`,
  `docs/groundedness-results.md` — generated from current code.
- ⬜ `docs/architecture.png` (diagram; the repo currently has textual diagrams only).
- ⬜ Demo video script: multi-turn conversation that triggers summary compression and ends
  with a genuine no-answer question.
- ⬜ Final submission pass: reproduce tests + eval from a clean `.venv`, tag/commit.

## Remaining work (as of Phase 7 close)
| Phase | State | Remaining deliverables |
|---|---|---|
| 8 — Observability & Hardening | ⬜ | LangChain callbacks; LLM cache; Redis cache; rate limiting + CORS; prompt-injection detection; Postgres advisory lock; finish logging/config polish |
| 9 — Docs & Submission | ⬜ | `docs/architecture.png`; demo video script; final README polish; clean-room test + eval verification; final commit |

Bottom line: **two phases remain** (8 and 9). Phase 8 is the largest remaining block;
Phase 9 is documentation/submission. The eval harness is complete, so submission numbers are
already generated from real runs.

## Milestones
- M1 (after Phase 2): retrieval works, eval numbers exist. ✅ **reached** —
  hybrid MRR 0.9214 vs 0.8548/0.8571 for the single retrievers, on 35 questions.
- M2 (after Phase 4): memory + graph end-to-end chat works. ✅ **reached** — a multi-turn
  incident thread compresses without losing ruled-out facts and the graph consumes the
  budgeted context.
- M3 (after Phase 7): eval harness + trace viewer. ✅ **reached** — viewer (Phase 6),
  ground-checked answers, groundedness and abstention measured from real runs (Phase 7).
- M4 (after Phase 9): submission-ready.

## Immediate next actions (Phase 8 entry)
1. LangChain callback handler: log latency + token counts for every LLM call, retrieval and
   chain step; keep the existing per-node trace as the user-facing view (no double counting).
2. Dev-only LLM response cache keyed by (provider, model, prompt, temperature, max_tokens,
   json_mode); disabled or TTL-bounded outside dev.
3. Redis caches for retrieval/embedding with the configured TTLs; `invalidate()` on ingest;
   a Redis outage must degrade to uncached, never fail the turn.
4. Rate limiting per IP/conversation from `guardrails.rate_limit_rps` (429 + Retry-After)
   and a CORS allowlist.
5. Prompt-injection detection: log/flag in dev, optional block under
   `guardrails.strict_injection_block`.
6. Config validation: cross-field checks (e.g. trigger >= working turns) and prod-only
   requirements; fail at startup, not on the first request.
7. Postgres advisory lock for per-conversation turns; document the single-process fallback.
8. Tests for cache hit/miss, Redis-down degradation, rate-limit 429, injection flag and
   invalid-config startup.

## Open questions to settle before Phase 8
- Cache keys must include every generation parameter; a model or temperature change must not
  return a stale stored answer.
- Callbacks are for operators, the trace is for the product. Keep them separate rather than
  turning one into a projection of the other.
- Rate limiting and locks are in-process today. Multi-instance correctness needs Postgres or
  Redis; document which guarantee applies where.
- Keep caching off in the eval harness so measured numbers are not flattered by cache hits.
