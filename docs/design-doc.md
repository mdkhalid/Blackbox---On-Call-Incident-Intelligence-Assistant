# Design Document — Blackbox

## 1. Purpose
Detailed design rationale for the Blackbox on-call incident intelligence assistant. Complements `high-level-design.md` (architecture overview).

## 2. Retrieval Design
- **Hybrid is mandatory**, not optional. Dense embeddings blur exact tokens (`ERR_POOL_EXHAUSTED`, `p99`, `checkout-service`); BM25 captures them. Semantics-only queries ("thing where requests pile up") need dense. One strategy cannot serve both.
- **Fusion**: Reciprocal Rank Fusion (RRF) default: `score = sum(1/(k + rank))`. Alternative weighted score. Weights/k are config-driven and justified by eval numbers (see retrieval-results.md). Never chosen by intuition.
- **Metadata filtering**: pushed into Chroma/Postgres `where` clauses before retrieval, not app-side post-filtering. Required for "SEV1 checkout incidents in last year".
- **Reranking**: cross-encoder (sentence-transformers) or LLM reranker. Measured with/without; if no gain on this corpus, documented and dropped with evidence.
- **Custom retriever**: subclass `BaseRetriever` so it composes with LCEL chains and LangGraph nodes.

## 3. Chunking Design
- Structure-aware per doc type. Postmortem sections (Summary, Impact, Timeline, Root Cause, Contributing Factors, Remediation, Action Items) are atomic units — Root Cause never split.
- Chunk sizes justified per type in `docs/chunking-strategy.md`.
- Metadata on every chunk enables filtered retrieval.
- Idempotent via deterministic IDs (hash doc_id + section).

## 4. Memory Design
- **Working**: last N turns verbatim (config).
- **Summary**: incident-aware compression. Prompt explicitly preserves: ruled-out hypotheses, confirmed facts, actions taken, timeline anchors. Generic summarizer rejected.
- **Entity**: structured facts (incident_id, services_implicated, hypotheses with status, actions_taken, timeline_anchors). Queried/injected every turn, separate from history.
- **Conflict resolution**: on contradiction, update status (ruled_out→confirmed) rather than append both. Demonstrated.
- **Budget**: explicit allocation across system/entity/summary/recent/chunks/output. Exposed via `/budget`.
- **Eviction order**: when over budget, evict in defined order (recent turns oldest first → summary compression → trim chunks). Justified and demonstrated.

## 5. Graph Design
Corrective RAG. Router → (entity lookup | retrieval | direct) → grade → (pass: generate; fail: rewrite bounded → re-retrieve) → ground_check → (pass: answer; fail: strip/flag) → honest_failure if exhausted.
- Rewrite cap default 2 (config).
- Ground check verifies each claim traces to a chunk.
- Honest failure states what was searched.

## 6. Guardrails
See `guardrails.md`. Includes: input validation, prompt injection defense, output filtering, tool/agent limits, retrieval grounding, rate limiting, PII redaction considerations.

## 7. Cost Control
See `cost-control.md`. LLM caching in dev, token budgets, model tiering (cheap model for grading/rewrite, stronger for generation), retrieval timeouts, rate limits, per-conversation cost tracking (bonus).

## 8. Security
See `api-security.md`. Keys in env only, never in code/logs; `.env` gitignored; secrets manager in prod; least privilege; no key in client.

## 9. Observability
LangChain callbacks log every LLM call, retrieval, chain step with latency + tokens. Structured JSON logs with request id. Trace viewer UI.

## 10. Config Discipline
All tunables in `config/config.yaml` + env overrides. No magic numbers in code.
