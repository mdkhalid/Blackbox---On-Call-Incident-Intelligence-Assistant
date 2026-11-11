# Development Stack — Blackbox

## Language & Runtime
- Python 3.11+
- Package manager: `uv` or `pip` + `pyproject.toml`

## Core Frameworks
- LangChain (langchain, langchain-core, langchain-community)
- LangGraph (langgraph)
- FastAPI + Uvicorn (SSE via sse-starlette)
- Pydantic v2 for schemas

## LLM / Embedding Providers
- Primary: OpenAI (gpt-4o-mini for grading/rewrite, gpt-4o for generation) OR Anthropic (claude-3-5-haiku / claude-sonnet-4). Pick one provider and stay consistent.
- Embeddings: OpenAI `text-embedding-3-small` (1536-dim) OR a local sentence-transformers model (`all-MiniLM-L6-v2`, 384-dim) for cost-free dev. Config-driven.
- Reranker: `cross-encoder/ms-marco-MiniLM-L-6-v2` (sentence-transformers) OR LLM-based reranker.

## Vector Store
- Chroma (chromadb) — local persistent, simple. Alternative: Qdrant or pgvector (config switch).

## Structured Data
- PostgreSQL (psycopg2 / SQLAlchemy) for: entity graph, conversations, turns, traces, eval results.
- Redis (redis-py) for: LLM response cache, retrieval cache, embedding cache, session state (optional but recommended).

## Keyword Search
- BM25 in-process, computed locally with Lucene's formula (fine for corpus size). Alternative: Postgres full-text.

## Observability
- structlog (JSON logging)
- LangChain callbacks (custom `BaseCallbackHandler`)
- Optional: LangSmith (set env, zero code change)

## Frontend (Trace Viewer)
- Minimal: plain HTML/CSS/JS served by FastAPI static. Optional: React/Vite if richer UI desired. Not a frontend assignment; keep minimal.

## Testing
- pytest + pytest-asyncio
- httpx for API tests

## Tooling
- ruff (lint/format), mypy (types)
- pre-commit hooks

## Environment Variables (`.env`, never committed)
```
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
DATABASE_URL=postgresql://...
REDIS_URL=redis://localhost:6379/0
CHROMA_PERSIST_DIR=./data/chroma
LLM_PROVIDER=openai
EMBEDDING_MODEL=text-embedding-3-small
GENERATION_MODEL=gpt-4o
GRADING_MODEL=gpt-4o-mini
REWRITE_MODEL=gpt-4o-mini
RERANKER_MODEL=cross-encoder/ms-marco-MiniLM-L-6-v2
LOG_LEVEL=INFO
ENV=dev
```

## Folder Structure
```
blackbox/
├── ingestion/{loaders,chunkers,entity_extraction,pipeline.py}
├── retrieval/{dense,sparse,fusion,rerank,hybrid_retriever}.py
├── graph/{state,nodes/,edges,build}.py
├── memory/{working,summary,entity,budget}.py
├── eval/{dataset.json,retrieval_eval,groundedness_eval}.py
├── api/
├── seed/
├── docs/
├── config/{settings.py,config.yaml}
├── tests/
└── README.md
```
