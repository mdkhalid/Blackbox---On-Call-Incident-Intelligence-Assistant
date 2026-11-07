# Phase-Wise Implementation Plan — Blackbox

On-Call Incident Intelligence Assistant. Python stack. Total budget 25–32 hrs.

## Phase 0 — Setup & Scaffolding (2 hrs)
- Create repo layout per spec (`blackbox/` tree).
- `pyproject.toml` with deps: langchain, langchain-openai/langchain-anthropic, langgraph, chromadb, rank_bm25, fastapi, uvicorn, sse-starlette, redis, psycopg2/sqlalchemy, pydantic, python-dotenv, tiktoken, structlog.
- `.env.example` listing all keys (never commit real keys). `.gitignore`.
- Config module (`config/settings.py`) reading env + YAML config file; all tunables (chunk sizes, fusion weights, top-k, rewrite cap, token budget) config-driven.
- Logging setup: structlog JSON, request-id contextvar.
- Health check endpoint.

## Phase 1 — Corpus & Ingestion (6 hrs)
- Generate 60+ internally consistent docs (30 postmortems, 15 runbooks, 10 arch docs, 5 alert defs) using a deterministic script (`seed/generate_corpus.py`) with shared service/error-code vocabulary.
- Loaders per doc type (`ingestion/loaders/`).
- Structure-aware chunkers per doc type (`ingestion/chunkers/`) — postmortem sections become atomic chunks; runbook symptom/diagnosis/mitigation/escalation; arch docs by section; alerts as single chunks.
- Metadata on every chunk: doc_type, services[], severity, date, section, error_codes[].
- Entity extraction at ingest → structured store (Postgres): services, error codes, dependencies graph.
- Idempotent re-ingestion: deterministic chunk IDs (hash of doc+section), upsert into Chroma + Postgres.
- Store raw docs in `seed/` (committed) or generation script.

## Phase 2 — Retrieval (5 hrs)
- Dense retriever (`retrieval/dense.py`): Chroma with chosen embedding model.
- Sparse retriever (`retrieval/sparse.py`): BM25 over chunk corpus (in-memory) OR Postgres FTS.
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
- Structured JSON logging with request id.
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
