# Phase-Wise Implementation Plan — Blackbox

On-Call Incident Intelligence Assistant. Python stack. Total budget 25–32 hrs.

Status: Phase 0 ✅ done, Phase 1 ✅ done, Phase 2 ⬅ next.

## Phase 0 — Setup & Scaffolding (2 hrs) ✅ DONE
- Repo layout, pyproject, config (env + YAML), structlog logging, request-id middleware, health endpoints.
- Dockerfile + docker-compose (app, postgres, redis). Tests: 15. ruff clean. Committed (ff7438f).

## Phase 1 — Corpus & Ingestion (6 hrs) ✅ DONE
- 60-doc deterministic corpus (30 PM, 15 RB, 10 arch, 5 alerts).
- Loaders, structure-aware chunkers (Root Cause atomic), metadata, entity extraction + SQLAlchemy store.
- Embedding providers (Fake/OpenAI/ST), Chroma wrapper (idempotent), ingestion pipeline (content-hash skip, batched).
- Tests: 23. ruff clean. Committed (ff7438f).

## Phase 2 — Retrieval (5 hrs) ⬅ NEXT
- Dense retriever (`retrieval/dense.py`): Chroma with chosen embedding model.
- Sparse retriever (`retrieval/sparse.py`): BM25 over chunk corpus (in-memory) or Postgres FTS.
- Fusion (`retrieval/fusion.py`): RRF (or weighted) with config-driven weights; parallel execution.
- Reranker (`retrieval/rerank.py`): cross-encoder or LLM reranker; measure impact.
- Custom LangChain retriever (`retrieval/hybrid_retriever.py`) wrapping all above; composes with LCEL.
- Metadata filtering pushed into Chroma/Postgres (not app-side).
- Retrieval timeout wrapper (asyncio timeout).

## Phase 3 — LangGraph Pipeline (5 hrs)
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

## Phase 7 — Eval Harness (3 hrs)
- `eval/dataset.json`: 25+ questions with known source docs.
- `retrieval_eval.py`: P@k/R@k/MRR across dense/sparse/hybrid/hybrid+rerank.
- `groundedness_eval.py`: claim support fraction.
- Results written to `docs/retrieval-results.md`.
- Re-runnable script; results in README.

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
- M1 (after Phase 2): retrieval works, eval numbers exist.
- M2 (after Phase 4): memory + graph end-to-end chat works.
- M3 (after Phase 7): eval harness + trace viewer.
- M4 (after Phase 9): submission-ready.
