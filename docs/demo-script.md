# Demo Video Script — Blackbox

Reproducible script for the submission recording. Every command below runs against the
seeded 60-document corpus with no API key (the offline provider is deterministic).

## 1. Setup

```bash
cp .env.example .env
python -m venv .venv
source .venv/bin/activate            # Windows: .\.venv\Scripts\Activate.ps1
pip install -e ".[dev,rag,db]"
pytest -q                            # 454 tests
uvicorn blackbox.main:app --reload
```

In a second terminal, seed the corpus once (idempotent):

```bash
curl -X POST localhost:8000/api/ingest \
  -H 'content-type: application/json' \
  -d '{"corpus_dir":"seed/corpus"}'
```

Ingestion is a background job, so the call answers `202` with a job id rather than the
ingest result. Poll the job to watch it finish:

```bash
curl -X POST localhost:8000/api/ingest \
  -H 'content-type: application/json' \
  -d '{"corpus_dir":"seed/corpus"}'
# -> {"job_id":"...","kind":"ingest","status":"queued","status_url":"/api/ingest/..."}

curl localhost:8000/api/ingest/<job_id>
# -> {"status":"succeeded","result":{"documents":60,"chunks":315,...}}
```

Open `http://localhost:8000/` for the trace viewer.

## 2. Scene: exact-token hybrid retrieval (live)

Ask:

> What caused ERR_POOL_EXHAUSTED in checkout-service?

Show in the viewer / SSE stream:

- graph path `router → retrieve → grade → generate → ground_check`;
- retrieved chunks with **dense, sparse, fused and rerank** scores (hybrid is doing the work;
  this exact-token case is why BM25 is present);
- the ground-check counts and the budget table.

## 3. Scene: multi-turn incident + compression + conflict resolution

Run the scripted conversation (it prints the conversation id):

```bash
python scripts/demo_chat.py --base-url http://localhost:8000
```

The script makes nine turns: it rules out the `01:15 deploy`, reports pool saturation and
`ERR_POOL_EXHAUSTED`, asks follow-ups, then says **"Actually, the 01:15 deploy was the
cause."**

After it finishes, load the printed conversation id in the viewer and show:

- `/memory` summary: the ruled-out deploy hypothesis survives compression;
- `/memory` entity: the deploy hypothesis appears **once**, status `confirmed`, with a
  history entry `ruled_out → confirmed` (no contradictory duplicate);
- `/budget`: the per-turn allocation for the last turn;
- `/trace`: the rewrite attempts and each turn's node timings.

## 4. Scene: honest failure (mandatory)

Ask, or use the final turn printed by the demo script:

> What is the SLA for the lunar telemetry pipeline?

Show `status: honest_failure` and the answer stating what was searched. The graph does not
invent a precedent; this is the measured `abstention_accuracy = 1.0` path.

## 5. Scene: evaluation numbers (optional, brief)

```bash
python -m blackbox.eval.retrieval_eval --k 5
python -m blackbox.eval.groundedness_eval
```

Show the tables in `docs/retrieval-results.md` and `docs/groundedness-results.md`:
hybrid MRR 0.9167 vs 0.8548/0.8714 for dense/sparse; answer rate 0.8857; abstention 1.0.

## 6. Closing talking points

- Structure-aware chunking (Root Cause atomic) and metadata filters compiled into Chroma.
- Fusion weights chosen from a measured sweep, not intuition.
- Memory preserves ruled-out facts and resolves contradictions in place.
- Ground check strips unsupported claims; no-answer questions fail honestly.
- Everything is config-driven; Redis and Postgres degrade gracefully.

## Notes for the recording

- The offline provider is extractive, so groundedness is a floor; say so if showing that
  table.
- Token events are emitted only after ground check, so the answer never appears unverified.
- If a rate limit trips during rapid scripted turns, wait a second or raise
  `guardrails.rate_limit_rps` in `blackbox/config/config.yaml`.
