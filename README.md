# Blackbox — On-Call Incident Intelligence Assistant

Ask questions against a corpus of past incidents, runbooks, architecture docs and alert
definitions: *"checkout p99 is spiking and the DB connection pool looks saturated — has this
happened before?"*

Phases 0–3 complete: scaffolding, corpus + ingestion, the retrieval layer, and the
corrective-RAG graph. See `docs/` for design and planning; `docs/phase-wise-plan.md` tracks
progress.

## Status

| Phase | Scope | State |
|---|---|---|
| 0 | Scaffolding, config, logging, health endpoints | ✅ |
| 1 | 60-document corpus, structure-aware chunking, entity extraction | ✅ |
| 2 | Hybrid retrieval, fusion, reranking, metadata filtering, eval harness | ✅ |
| 3 | LangGraph corrective-RAG graph: routing, grading, bounded rewrite, ground check | ✅ |
| 4 | Three-layer memory, conflict resolution, token budget | next |

## Quick start

```bash
cp .env.example .env
pip install -e ".[dev,rag]"
pytest -q                                   # 205 tests
uvicorn blackbox.main:app --reload
```

No API key is required to run retrieval. Dense embeddings fall back to Chroma's bundled
ONNX MiniLM model, so the corpus, the tests and the eval harness all work offline. Set
`OPENAI_API_KEY` and `retrieval.embedding_provider: openai` to switch to a hosted model.

## Run with Docker

```bash
cp .env.example .env
docker compose up --build
curl http://localhost:8000/health
```

## Retrieval

`blackbox/retrieval/` — dense and sparse retrieval run in parallel, fuse, then optionally
re-rank. Every stage records its own score and rank on each chunk, which is what the trace
viewer renders.

| File | Role |
|---|---|
| `dense.py` | Cosine similarity over Chroma; distance converted to similarity |
| `sparse.py` | In-process BM25 with an identifier-preserving tokenizer |
| `fusion.py` | RRF and normalized weighted sum, both config-driven |
| `rerank.py` | Lexical, cross-encoder, LLM, or null — selectable and measured |
| `filters.py` | Metadata filters compiled to native Chroma predicates |
| `hybrid_retriever.py` | The custom LangChain `BaseRetriever` composing all of the above |
| `timeout.py` | Deadline guard so a slow store cannot hang a request |

Everything is configurable in `blackbox/config/config.yaml` — weights, top-k, fusion method,
rerank provider, timeout — with no code changes.

### Using it

```python
from blackbox.retrieval import build_default_retriever, MetadataFilter

retriever = build_default_retriever()

hits = retriever.retrieve("ERR_POOL_EXHAUSTED in checkout-service", top_k=5)
for h in hits:
    print(h.metadata["doc_id"], h.metadata["section"], round(h.score, 4))

# Filtering happens inside the store, not after the fact.
sev1 = retriever.retrieve(
    "connection pool exhaustion",
    top_k=5,
    filter=MetadataFilter(services=["checkout-service"], severities=["SEV1"]),
)

# It is a real LangChain retriever, so it composes.
from langchain_core.runnables import RunnableLambda
chain = retriever | RunnableLambda(lambda docs: [d.metadata["doc_id"] for d in docs])
```

### Retrieval results

Measured on the seeded 60-document corpus with a 35-question eval set. Full table,
per-category breakdown and the fusion weight sweep are in
[`docs/retrieval-results.md`](docs/retrieval-results.md).

| Strategy | MRR | Recall@5 | Precision@5 |
|---|---|---|---|
| dense-only | 0.8548 | 0.8581 | 0.4433 |
| sparse-only (BM25) | 0.8571 | 0.8510 | 0.3510 |
| hybrid (RRF) | **0.9214** | **0.9067** | **0.4548** |
| hybrid + rerank | 0.8667 | 0.9219 | 0.4543 |

Two results are reported as measured rather than as hoped:

- **Reranking is disabled by default.** It lowered MRR by 0.055 on this corpus while
  raising recall. It is implemented and selectable per request.
- **Dense-only beats BM25 on exact-token queries here** (0.90 vs 0.85), contrary to the
  usual expectation. The seeded documents are short and templated, so identifiers appear in
  tight, repeated contexts that MiniLM encodes well. The reasoning and the data are in
  `docs/retrieval-results.md`.

Reproduce:

```bash
python scripts/build_eval_dataset.py          # derive the question set from the corpus
python -m blackbox.eval.retrieval_eval --k 5 --write --fusion-sweep
```

## Graph

`blackbox/graph/` — a corrective-RAG pipeline built as a LangGraph `StateGraph`:

```
router ─┬─ entity ── entity_lookup ─────────────────┐
        ├─ direct ── direct_answer ── END           │
        └─ retrieve ── grade ─┬─ sufficient ────────┤
                    ▲         ├─ rewrite ── retrieve│
                    └─────────┴─ cap reached ── honest_failure
                                                       │
                        generate ── ground_check ──────┤
                                    └─ ungrounded ─────┘
```

| File | Role |
|---|---|
| `state.py` | JSON-serialisable state; every chunk keeps its full retrieval score trail |
| `router.py` | Deterministic routing; defaults to retrieval, entity lookup only for known-service dependency questions, direct only for greetings/capabilities |
| `nodes.py` | Retrieve, grade, rewrite, generate, ground check, honest failure |
| `grounding.py` | Deterministic claim→chunk tracing; unsupported claims are stripped |
| `build.py` | Graph assembly plus sync and streaming runners |

Every node appends a timing/decision event to `state["trace"]`, and
`stream_graph_events()` yields those events as they happen for the API and trace viewer.
Rewrites are bounded by `retrieval.rewrite_cap` (default 2); hitting the cap produces an
honest failure that states the query, filters and what was searched.

```python
from blackbox.graph import build_graph, initial_state, run_graph
from blackbox.retrieval import build_default_retriever

retriever = build_default_retriever()
graph = build_graph(retriever)          # offline provider when no API key is set
result = run_graph(graph, initial_state("What caused ERR_POOL_EXHAUSTED in checkout-service?"))

print(result["status"])                 # answered | honest_failure | direct
print(result["answer"])
print([event["node"] for event in result["trace"]])
```

`blackbox/llm/` is the provider seam: `complete()` is the only method the graph calls,
with OpenAI and Anthropic implementations plus a deterministic offline provider (lexical
grading, extractive generation, identifier-preserving rewriting). `LLM_PROVIDER=openai`,
`anthropic`, `fake` or `auto` selects it; `auto` falls back to the offline provider when no
key is present in dev and fails loudly in prod.

## Corpus

`seed/corpus/` — 60 deterministic documents (30 postmortems, 15 runbooks, 10 architecture
docs, 5 alert definitions), regenerable via `python seed/generate_corpus.py`. All four
document types share one service/error-code vocabulary so retrieval is measurable.

## Tests

205 tests, `ruff` clean. Retrieval and graph tests run against a small in-file corpus with
a deterministic hashing embedder and the offline LLM provider, so they need no network and
no model download.

```bash
pytest -q
python -m ruff check blackbox tests scripts
```
