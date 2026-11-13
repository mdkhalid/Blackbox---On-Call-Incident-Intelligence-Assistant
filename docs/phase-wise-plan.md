# Phase-Wise Implementation Plan — Blackbox

On-Call Incident Intelligence Assistant. Python stack. Total budget 25–32 hrs.

Status: Phase 0 ✅, Phase 1 ✅, Phase 2 ✅, Phase 3 ✅, Phase 4 ✅, Phase 5 ✅, Phase 6 ✅, Phase 7 ✅, Phase 8 ✅, Phase 9 ✅. **All phases complete.**

Environment: project-local `.venv` at `.venv/`; all dependencies installed there, none globally. Driver is `psycopg[binary]` because SQLAlchemy 2.1 resolves `postgresql://` to psycopg 3. Tests: **465 passing** (9 skipped without a live OpenSearch cluster), `ruff` clean, `mypy` clean.

## Production Program (post-assignment)
The original assignment is complete. Further work is tracked separately:
- [`scaling-and-production-readiness.md`](scaling-and-production-readiness.md) — target
  architecture, capacity/SLOs, tenancy, retrieval migration, Kubernetes and multi-region plan.
- [`development-tracker.md`](development-tracker.md) — living checklist, status dashboard,
  decisions and change log. Update it with every production PR.
Current production state: **Phase 4 (Kubernetes and operations) in progress**. Phase 3
(async runtime and jobs) is complete. Phase 4 has landed the Helm deployment package, the AWS
Terraform reference (EKS/Aurora/ElastiCache/S3/KMS/Secrets Manager/IRSA), unprivileged image,
Prometheus metrics with SLO/error-budget alert rules, External Secrets wiring, and the CI
coverage/image/SBOM plus Terraform validate/scan gates; OpenTelemetry, Grafana/Loki/Tempo,
GitOps, load/chaos and the runbooks remain. Phase 2 open items are the live-cluster backend
benchmark and global quotas (Phase 4).

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
| sparse-only (BM25) | 0.8714 | 0.8510 | 0.3786 |
| hybrid (RRF) | **0.9167** | **0.8867** | **0.4348** |
| hybrid + rerank | 0.8857 | 0.9219 | 0.4543 |

### Decisions the numbers forced (not the defaults)
- **Fusion weights 0.75/0.25, from a 21-point sweep.** 0.75–0.80 is a flat plateau at MRR
  0.9167; 0.75 picked mid-plateau, not at an arbitrary argmax. Justified, not intuited.
- **The metric's last decimal place is not a finding.** The BM25 leg returns groups of
  documents that score identically, and the order inside a group is decided by the
  tie-break. Re-running the hybrid with only the tie-break changed moves MRR by 0.0286
  (`--tie-break-sweep`), six times the difference between the shipped sparse backend and
  the OpenSearch one. The shipped tie-break is by document id, because it is the only
  policy derived from the chunk's own content rather than from a store's internal order.
- **Reranking disabled by default.** It costs 0.031 MRR here while gaining 0.035 recall:
  it lifts single-doc `structural` questions but drops `exact_token` ones, because runbooks
  state the service+code pair more compactly than the postmortem does. Kept implemented and
  selectable per request so it can be re-measured on a real corpus.
- **Dense and BM25 tie on exact-token queries (0.90 vs 0.90)** — the assignment's premise
  was that dense would fail here. Cause is corpus shape (short, templated docs give
  identifiers a tight, repeated context). Documented as measured rather than re-labelled to
  match expectations. Hybrid
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
  regenerated results are 0.9167 → 0.8857.

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
  service; direct only for greetings, capabilities and glossary definitions; everything
  else retrieves.
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
- **Direct answers are restricted to meta questions** and are templated (greetings,
  capabilities, glossary definitions such as p99/SLO), so they cannot hallucinate incident
  facts. Incident questions always retrieve.
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
  answers `202 {job_id}` and runs on a background worker) and `POST /api/eval/run` (same
  shape; serialised metrics JSON, optional fusion sweep; does not rewrite docs). Status:
  `GET /api/jobs/{id}`, `GET /api/ingest/{id}`, `GET /api/eval/{id}`.
- Budget wiring: graph evidence fitting now uses the same tiktoken counter as memory, and
  `/budget` reports the request that was actually sent, including the exact prompt token
  count.
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

## Phase 8 — Observability, Caching, Hardening ✅ DONE
- `cache/`: `TTLCache` and `RedisCache` backends with best-effort semantics (a failure is a
  miss), plus `CachedEmbeddingProvider`.
- Retrieval cache in `HybridRetriever`: keyed by query, top-k, filters, strategy flags and
  fusion/rerank config; TTL from `redis.ttl_retrieval_s`; cleared by `invalidate()` on
  ingest; a hit records `cache_ms` in the stage timings.
- Dev LLM response cache (`CachedLLMProvider`) keyed by provider, model, system, prompt,
  temperature, max tokens and JSON mode. Disabled in prod and not used by the eval
  harnesses, so measured numbers are not flattered by cache hits.
- Observability: LangChain-compatible `LoggingCallbackHandler` (LLM, retriever, chain
  latency/tokens) plus structured `llm_call` logs from the provider seam with node, model,
  tokens, cached flag and duration. The graph trace remains the product-facing view.
- Rate limiting: per-IP sliding window from `guardrails.rate_limit_rps` (429 + Retry-After);
  health and the viewer are exempt. CORS allowlist from `CORS_ORIGINS`; `*` is rejected in
  prod.
- Prompt-injection heuristics flag and log by default; `strict_injection_block` rejects with
  400. Flags are carried into the final SSE payload.
- Turn locking: asyncio lock for single-process SQLite/dev, Postgres session advisory lock
  when the conversation store is Postgres; 409 when a conversation is already busy.
- Config validation: memory trigger must exceed the working window, positive guardrails,
  non-negative budgets/TTLs, bounded relevance score, prod CORS rule.
- Tests: 286 total (+32), ruff clean.

### Verified
- Local smoke with an unreachable Redis: warning logged, in-memory fallback used, chat
  end-to-end unchanged (SSE, trace, persistence, budget).
- Unit coverage for TTL expiry/prefix clearing, Redis failure-as-miss, embedding cache
  hits, LLM cache parameter separation, retrieval cache invalidation, 429 + Retry-After,
  health exemption, CORS preflight, strict injection rejection and advisory-lock acquire /
  busy / release.

### Decisions
- **Redis is never required.** Every cache path degrades to memory or a miss; the DB stays
  the source of truth.
- **LLM caching is dev-only** and keys include every generation parameter, so a model or
  temperature change cannot serve a stale answer.
- **Rate limiting is in-process**, matching the current single-instance deployment. Moving
  counters to Redis is the documented multi-instance next step.
- **Injection detection flags rather than blocks by default**: incident text can legitimately
  contain words like "system" or "instructions", so false positives would harm real users.

## Phase 9 — Docs & Submission ✅ DONE
- `docs/architecture.png`: full component/flow diagram, generated reproducibly by
  `scripts/render_architecture.py` (`pip install -e ".[docs]"`).
- `docs/demo-script.md`: scene-by-scene recording script. `scripts/demo_chat.py` reproduces
  the nine-turn incident conversation and the no-answer question over the real HTTP API.
- README: all nine phases, architecture embed, both results tables, API/hardening notes,
  submission checklist and known limitations.
- Entity memory is now updated from every chat turn (services, error codes, timeline
  anchors, actions, and hypothesis status from explicit user statements), so conflict
  resolution is demonstrable through the API and viewer, not only in unit tests.
- Clean-room verification in a fresh venv at a short path (`.[dev,rag,db]`):
  - 295 tests pass, ruff clean;
  - corpus ingested from `seed/corpus`: 60 documents → 315 chunks;
  - retrieval eval reproduced exactly (hybrid MRR 0.9167 / R@5 0.8867 / P@5 0.4348);
  - groundedness subset (10 answerable + 5 unanswerable): answer rate 1.0, abstention 1.0,
    draft claim support 1.0;
  - live demo over HTTP: compression preserved the ruled-out deploy, the entity hypothesis
    resolved `ruled_out → confirmed` with history, and the no-answer question took the
    honest-failure path.
- Tests: 300 total (295 at Phase 9 close, +5 from the audit), ruff clean.

### Post-submission audit fixes
- Entity-memory conflict matching now strips leading articles, so "The deploy was ruled
  out" followed by "actually the deploy was the cause" updates one hypothesis instead of
  holding two contradictions.
- Service extraction uses an infrastructure-suffix allowlist; prose like `code-review` and
  `read-only` no longer pollutes entity memory.
- Memory state and the finished turn are persisted in one transaction (`record_turn`).
- The built wheel now includes `config/config.yaml`, `eval/dataset.json` and
  `api/static/index.html`; the Dockerfile copies the package before the editable install.
- `/health/ready` actually checks the conversation store (503 when unavailable).
- Router answers glossary definitions (p99, SLO, SEV1, runbook, …) directly without
  retrieval, while "What is the SLA for X?" still retrieves.
- Rate-limiter IP map is bounded; the offline provider labels its model `fake` rather than
  impersonating the configured hosted model.

### Decisions
- **The diagram is generated, not hand-drawn**, so it cannot silently drift from the docs.
- **The demo is a script, not a checklist**, so the recording is reproducible and exercises
  compression, conflict resolution and honest failure on the real API.
- **Entity updates read only the user's message** for hypothesis status: the assistant must
  never confirm its own guess. Grounded claims remain the graph's responsibility.
- **Known limitations are in the README**, not hidden: decode-level token streaming, offline
  groundedness as a floor, single-instance rate limiting, templated `paraphrase` weakness.

## Post-submission (optional bonus ideas)
- Temporal weighting ("third time this quarter") and service aliases.
- Related-incidents sidebar from entity overlap rather than text similarity.
- Feedback capture stored against the retrieval that produced each answer.
- Per-conversation cost report from the existing usage counters.
- True provider token streaming with a shared JSON/answer streaming protocol.

## Remaining work
**None. All phases are complete.** The submission assets are the README, the four generated
docs files, `docs/architecture.png`, `docs/demo-script.md`, the seeded corpus and the
re-runnable eval scripts.

## Milestones
- M1 (after Phase 2): retrieval works, eval numbers exist. ✅ **reached** —
  hybrid MRR 0.9167 vs 0.8548/0.8714 for the single retrievers, on 35 questions.
- M2 (after Phase 4): memory + graph end-to-end chat works. ✅ **reached** — a multi-turn
  incident thread compresses without losing ruled-out facts and the graph consumes the
  budgeted context.
- M3 (after Phase 7): eval harness + trace viewer. ✅ **reached** — viewer (Phase 6),
  ground-checked answers, groundedness and abstention measured from real runs (Phase 7).
- M4 (after Phase 9): submission-ready. ✅ **reached** — diagram, demo script, clean-room
  verification and submission checklist.

## Final verification commands
```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .\.venv\Scripts\Activate.ps1
pip install -e ".[dev,rag,db]"
pytest -q                            # 465 tests
python -m blackbox.eval.retrieval_eval --k 5 --write --fusion-sweep --tie-break-sweep
python -m blackbox.eval.groundedness_eval --write
uvicorn blackbox.main:app --reload   # then POST /api/ingest and open /
```
