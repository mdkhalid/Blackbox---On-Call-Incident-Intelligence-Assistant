# Blackbox — On-Call Incident Intelligence Assistant

Ask questions against a corpus of past incidents, runbooks, architecture docs and alert
definitions: *"checkout p99 is spiking and the DB connection pool looks saturated — has this
happened before?"*

Phases 0–2 complete: scaffolding, corpus + ingestion, and the retrieval layer.
See `docs/` for design and planning; `docs/phase-wise-plan.md` tracks progress.

## Status

| Phase | Scope | State |
|---|---|---|
| 0 | Scaffolding, config, logging, health endpoints | ✅ |
| 1 | 60-document corpus, structure-aware chunking, entity extraction | ✅ |
| 2 | Hybrid retrieval, fusion, reranking, metadata filtering, eval harness | ✅ |
| 3 | LangGraph corrective-RAG pipeline | next |

## Quick start

```bash
cp .env.example .env
pip install -e ".[dev,rag]"
pytest -q                                   # 164 tests
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
| dense-only | 0.8548 | 0.8581 | 0.4662 |
| sparse-only (BM25) | 0.8571 | 0.8510 | 0.3738 |
| hybrid (RRF) | **0.9214** | **0.9162** | **0.4910** |
| hybrid + rerank | 0.8643 | 0.9219 | 0.4771 |

Two results are reported as measured rather than as hoped:

- **Reranking is disabled by default.** It lowered MRR by 0.057 on this corpus while
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

## Corpus

`seed/corpus/` — 60 deterministic documents (30 postmortems, 15 runbooks, 10 architecture
docs, 5 alert definitions), regenerable via `python seed/generate_corpus.py`. All four
document types share one service/error-code vocabulary so retrieval is measurable.

## Tests

164 tests, `ruff` clean. Retrieval tests run against a small in-file corpus with a
deterministic hashing embedder, so they need no network and no model download.

```bash
pytest -q
python -m ruff check blackbox tests scripts
```
