# Guardrails — Blackbox

## Input Guardrails
- **Length limit**: message ≤ config `max_message_chars` (default 4000). Reject 413 otherwise.
- **Empty/whitespace**: reject 400.
- **Prompt injection detection**: regex + heuristic for "ignore previous instructions", "system:", etc. Flag but do not hard-block (log + continue with hardened prompt); optionally block in strict mode.
- **PII detection (optional)**: detect emails/phones/keys; redact or reject per config.
- **Rate limiting**: per IP and per conversation (slowapi); configurable RPS.

## Retrieval Guardrails
- **Metadata filter validation**: validate filter values against allowed enums (doc_type, severity, service names) to prevent injection into query.
- **Timeout**: retrieval must complete within `retrieval_timeout_ms`; else return empty + `retrieval_timed_out: true`.
- **Top-k cap**: config max (e.g. 20) regardless of request.
- **Score threshold**: drop chunks below `min_relevance_score` before generation (configurable; default off, documented).

## Generation Guardrails
- **Grounding**: ground_check node verifies each factual claim traces to a retrieved chunk. Unsupported claims removed or flagged (`[unverified]`).
- **Honest failure**: if no relevant chunks after bounded retries, respond plainly: "I didn't find a matching past incident. I searched: <query>, <filters>." Never fabricate precedent.
- **Citation**: answer must cite source doc IDs/sections for claims (format: [doc_id#section]).
- **No tool execution**: LLM has no tools in v1; cannot take actions; only answers. (Future: gated tools.)
- **Output length cap**: max tokens for answer (config).

## Output Guardrails
- **Secret scrubbing**: regex scrub for API-key-like patterns, emails (optional), internal URLs (optional) in final answer and logs.
- **Refusal patterns**: if model refuses or hedges excessively, pass through but flag in trace.
- **JSON validity**: structured outputs (entity memory, grading) validated with Pydantic; on parse failure, retry once then fail safe.

## Agent/Graph Guardrails
- **Bounded loops**: rewrite cap (default 2). No unbounded cycles.
- **Cost cap per request**: max LLM calls per turn (config, e.g. 6); abort with partial + error if exceeded.
- **Token budget**: hard cap on total context; eviction enforced.
- **Concurrency**: one active graph run per conversation (lock).

## Monitoring & Alerts
- Log guardrail triggers with request id.
- Metric: guardrail trigger rate; alert if spike (optional).
- Eval: track hallucination rate (groundedness) in eval harness.

## Configuration
All guardrail thresholds in `config/config.yaml`:
```yaml
guardrails:
  max_message_chars: 4000
  rate_limit_rps: 5
  retrieval_timeout_ms: 5000
  max_llm_calls_per_turn: 6
  max_answer_tokens: 1024
  strict_injection_block: false
  secret_scrub: true
```
