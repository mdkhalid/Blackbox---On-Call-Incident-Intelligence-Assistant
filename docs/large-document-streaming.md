# Large Document Handling & Streaming Ingestion — Blackbox

## Problem
Corpus documents can be large (multi-MB postmortems, large architecture docs). Loading an entire file into memory, then all chunks, then all embeddings, can exhaust memory and OOM the worker. We need streaming ingestion with bounded memory.

## Design Principles
1. **Never load a whole document into memory if avoidable.** Stream parse in chunks.
2. **Never hold all chunks/embeddings in memory.** Process and persist in bounded batches.
3. **Bounded memory**: peak memory independent of document size.
4. **Progress visibility**: stream ingestion progress to client via SSE.
5. **Resumable/idempotent**: partial failures recoverable.

## Upload Handling
- FastAPI `UploadFile` is already a SpooledTemporaryFile; read in chunks:
  ```python
  async with aiofiles.open(tmp_path, "wb") as out:
      while chunk := await upload.read(1024*1024):  # 1MB
          await out.write(chunk)
  ```
- Files above `spool_threshold` (default 1MB) spill to disk automatically; never fully in RAM.
- Enforce `max_upload_size` (default 50MB/file) and `max_total_corpus_size` (default 2GB). Reject 413 otherwise.
- Validate content type and magic bytes; reject non-text/binary disguised as text.

## Streaming Parsers
- Text/Markdown: read line-by-line (`open(...).readline` or async line iterator); accumulate section buffers, yield sections as complete.
- PDF (if used): page-by-page via `pypdf.PdfReader` (lazy) or `pdfplumber`; never load all pages.
- DOCX: stream `document.xml` parts via `python-docx` (iterates paragraphs lazily) or zipfile streaming.
- Corpus is Markdown, so primary path is line-streaming.

## Generator-Based Chunking
```python
def chunk_document(path) -> Iterator[Chunk]:
    with open(path, "r", encoding="utf-8") as f:
        for section in stream_sections(f):   # yields (heading, lines)
            for chunk in split_section(section):  # yields Chunk
                yield chunk
```
- Chunkers are generators; they do not materialize the full chunk list.
- Each `Chunk` is a small dataclass (text + metadata), bounded size.

## Bounded Embedding & Upsert
- Accumulate chunks into a batch (default 64 or ~100k tokens).
- When batch full: embed batch → upsert to Chroma → discard batch from memory.
- Track progress counters (chunks_processed, batches_done).
- Chroma upsert in batches (default 100) to avoid large single writes.

## Memory Budget for Ingestion
- `ingestion_batch_size_chunks` (default 64)
- `ingestion_batch_size_tokens` (default 100000)
- `max_in_flight_embeddings` (default 1 batch)
- Backpressure: if embedding provider rate-limits, sleep/backoff; do not queue unbounded.

## Async / Background Ingestion
- `POST /api/ingest` accepts files and returns `job_id` immediately (202 Accepted).
- Ingestion runs in a background task (FastAPI `BackgroundTasks` for small; ARQ/Celery/RQ for prod).
- Client polls `GET /api/ingest/:job_id` or subscribes to SSE progress stream.
- Job state persisted in Postgres (`ingest_jobs` table): status, progress, error, started_at, finished_at.
- Idempotent: re-running a job with same content hash skips.

> **Built (2026-11-11).** `POST /api/ingest` and `POST /api/eval/run` answer `202 {job_id}`
> and run on a dedicated worker thread; `GET /api/jobs/:id`, `GET /api/ingest/:id` and
> `GET /api/eval/:id` report the job's status and result. Two jobs of the same kind are
> queued rather than refused.
>
> The worker is a **thread**, not a task on the request's event loop: a request's loop does
> not outlive the request, so a task-based worker would be cancelled the moment the response
> was sent.
>
> Still to come from this section: the job state is in-process, so it does not survive a
> restart and is not shared between replicas — that is the Postgres `ingest_jobs` table and
> an ARQ/Celery/RQ worker. The SSE progress stream is not implemented either; a job reports
> its state when polled.

## Progress Streaming (SSE)
Events during ingestion:
- `{"stage": "upload_received", "bytes": N}`
- `{"stage": "parsing", "doc": "pm-001"}`
- `{"stage": "chunking", "chunks_so_far": N}`
- `{"stage": "embedding", "batch": i, "of": total}`
- `{"stage": "upserting", "chunks": N}`
- `{"stage": "entity_extraction", "doc": "pm-001"}`
- `{"stage": "done", "docs": N, "chunks": M}`
- `{"stage": "error", "message": ...}`

## Failure & Recovery
- Per-document try/except: one bad doc does not abort the whole job; record error, continue.
- Checkpoint after each document: job can resume from last successful doc (store `last_doc_id` in job state).
- Idempotent chunk IDs ensure re-runs don't duplicate.
- Temp files cleaned up in `finally` (and on startup sweep of stale temp files).

## Large Single Document (e.g., 100MB markdown)
- Stream parse; chunk; embed batch; upsert batch; never hold whole doc.
- If a single section exceeds max chunk size, split by paragraph/sentence with bounded sub-chunks.
- Warn if document has no headings (poor structure) — chunk by fixed size with overlap as fallback.

## Concurrency
- Limit concurrent ingest jobs (default 2) via semaphore/Redis lock to avoid memory spikes.
- Per-job memory cap via process limits (ulimit/container limits) in prod.

## Monitoring
- Track peak RSS during ingest (optional, via `resource` module) and log.
- Alert if ingest job exceeds time/memory threshold.

## Configuration
```yaml
ingestion:
  max_upload_size_mb: 50
  max_total_corpus_size_mb: 2048
  spool_threshold_bytes: 1048576
  batch_size_chunks: 64
  batch_size_tokens: 100000
  max_concurrent_jobs: 2
  checkpoint_per_doc: true
  temp_dir: ./data/tmp
```

## Edge Cases
- Empty file: skip with warning, job continues.
- File with no valid UTF-8: try latin-1 fallback; if still fails, record error.
- Truncated upload: checksum/size check; reject.
- Duplicate upload of same file: idempotent; return cached job result.
- Out of disk: fail job with clear error; clean temp files.
- Very long line (no newlines): split by max line length to avoid unbounded buffer.
