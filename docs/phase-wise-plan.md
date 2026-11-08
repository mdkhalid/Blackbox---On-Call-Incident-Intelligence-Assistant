# Phase-Wise Implementation Plan — Blackbox

On-Call Incident Intelligence Assistant. Python stack. Total budget 25–32 hrs.

Status: Phase 0 ✅, Phase 1 ✅, Phase 2 ✅ done. Phase 3 ⬅ next.

## Phase 0 — Setup & Scaffolding (2 hrs) ✅ DONE
- Repo layout, pyproject, config (env + YAML), structlog logging, request-id middleware, health endpoints.
- Dockerfile + docker-compose (app, postgres, redis). Tests: 15. ruff clean. Committed (ff7438f).

## Phase 1 — Corpus & Ingestion (6 hrs) ✅ DONE
- 60-doc deterministic corpus (30 PM, 15 RB, 10 arch, 5 alerts).
- Loaders, structure-aware chunkers (Root Cause atomic), metadata, entity extraction + SQLAlchemy store.
- Embedding providers (Fake/OpenAI/ST), Chroma wrapper (idempotent), ingestion pipeline (content-hash skip, batched).
- Tests: 23. ruff clean. Committed (ff7438f).

## Phase 2 — Retrieval (5 hrs) ✅ DONE (code complete, uncommitted)
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
| dense-only | 0.8548 | 0.8581 | 0.4662 |
| sparse-only (BM25) | 0.8571 | 0.8510 | 0.3738 |
| hybrid (RRF) | **0.9214** | **0.9162** | **0.4910** |
| hybrid + rerank | 0.8643 | 0.9219 | 0.4771 |

### Decisions the numbers forced (not the defaults)
- **Fusion weights 0.75/0.25, from a 21-point sweep.** 0.55–0.80 is a flat plateau at MRR
  0.9214; 0.75 picked mid-plateau, not at an arbitrary argmax. Justified, not intuited.
- **Reranking disabled by default.** It costs 0.057 MRR here while gaining 0.006 recall:
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

## Phase 3 — LangGraph Pipeline (5 hrs) ⬅ NEXT
- State schema (`graph/state.py`).
- Nodes: router, grade, rewrite, generate, ground_check, honest_failure.
- Edges: conditional routing.
- Build graph (`graph/build.py`) with streaming events.
- Router: entity/graph lookup vs retrieval vs direct answer.
- Grading node: LLM judges whether chunks answer question.
- Rewrite cycle bounded by config cap; on cap → honest_failure.
- Ground check: verify claims trace to chunks; strip/flag unsupported.
- Honest-failure path: plain statement of what was searched.

## Phase 4 — Memory (4 hrs)
- Working memory: recent turns verbatim.
- Summary memory: incident-aware compression prompt + trigger policy.
- Entity memory: structured facts + conflict resolution.
- Budget: allocation + eviction order; `/budget` API.
- Injection on every turn separate from history.

## Phase 5 — API + Streaming (3 hrs)
- FastAPI routes per spec.
- SSE streaming of graph events + tokens.
- Conversation persistence (Postgres).
- Trace storage for `/trace`.
- `/memory`, `/budget`, `/ingest`, `/eval/run`.

## Phase 6 — Trace Viewer UI (2 hrs)
- Minimal web UI (HTML/JS or small React) showing per-message: graph path, retrieved chunks with scores, grading result, ground check, budget breakdown.

## Phase 7 — Eval Harness (3 hrs) ◐ PARTIAL
- ✅ `eval/dataset.json`: 35 questions with corpus-derived source docs (built in Phase 2).
- ✅ `retrieval_eval.py`: MRR/P@k/R@k across dense/sparse/hybrid/hybrid+rerank (Phase 2).
- ✅ Results written to `docs/retrieval-results.md`; reproduction command in README (Phase 2).
- ⬜ `groundedness_eval.py`: claim support fraction — needs the Phase 3 generator first.
- ⬜ Final table refresh once the graph produces real answers (Phase 5 API `/eval/run`).

## Phase 8 — Observability, Caching, Hardening (2 hrs)
- LangChain callbacks: latency + token counts per LLM call/retrieval/chain.
- LLM response caching (dev).
- Structured JSON logging with request id (partial done in Phase 0).
- Config validation.
- Redis cache integration for retrieval/embedding caches.

## Phase 9 — Docs & Submission (1 hr)
- Write the four required docs files + README with setup/architecture/comparison table.
- Demo video script (multi-turn conversation triggering compression + no-answer question).

## Milestones
- M1 (after Phase 2): retrieval works, eval numbers exist. ✅ **reached** —
  hybrid MRR 0.9214 vs 0.8548/0.8571 for the single retrievers, on 35 questions.
- M2 (after Phase 4): memory + graph end-to-end chat works.
- M3 (after Phase 7): eval harness + trace viewer. (Harness half already done in Phase 2.)
- M4 (after Phase 9): submission-ready.

## Immediate next actions (Phase 3 entry)
1. `graph/state.py` — typed state carrying query, filter, chunks + score trails, grade
   history, rewrite count, claims, and the trace buffer the viewer will render.
2. Nodes in dependency order: router → retrieve → grade → (rewrite, bounded) → generate →
   ground_check → honest_failure. Grade and rewrite are cheap-model calls; generate is not.
3. Wire `HybridRetriever.retrieve()` straight into the retrieve node — it already returns
   per-stage scores, so the trace needs no extra plumbing.
4. Stream node-level events from the start; retrofitting streaming into a finished graph is
   worse than designing for it.
5. Reuse `config.retrieval.rewrite_cap` (currently 2) as the bound, and make hitting the cap
   route to honest_failure rather than raising.

## Open questions to settle before Phase 4
- Does entity memory need its own store, or is it a field in conversation state? (Leaning
  state field + Postgres persistence in Phase 5, so the graph stays pure.)
- Summarisation must be demonstrably incident-aware. Needs a test that a ruled-out
  hypothesis survives compression — write it as a failing test first.
