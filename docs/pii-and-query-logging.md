# PII and Query-Logging Policy

Incident text is user data. It legitimately contains customer identifiers, internal
hostnames, IPs and occasionally credentials pasted from an alert. Logs are retained
longer and read by more people than the conversation store, so they get a narrower
policy.

## Controls

| Control | Env | Default | Behaviour |
|---|---|---|---|
| PII redaction | `PII_REDACTION_ENABLED` | `true` | `redact_pii` masks emails, IPv4 addresses, `Bearer`/`Basic` credentials and provider-looking tokens (`sk-…`, `ghp_…`, `xox…`). |
| Query log mode | `QUERY_LOG_MODE` | `hash` | What a query log records: `hash` (truncated SHA-256, correlatable but opaque), `redact` (text with PII masked), `raw` (verbatim; explicit opt-in). |

`blackbox/pii.py` implements both. The chat route logs `chat_query` at debug level with
the policy applied; the raw user message is not written to a log line under the default
policy.

## Why hash by default

A hash preserves the two things an operator needs — "are these the same query?" and
"how often does it appear?" — without storing the content. It also keeps the log line a
fixed length, so a long pasted stack trace cannot bloat the log stream.

## Choosing `redact`

Use `redact` when query text is needed to debug retrieval quality and the deployment
is not regulated. It keeps words like `p99` and error codes while masking the
identifiers that make a log a compliance problem.

Use `raw` only where the log store is access-controlled and the data is not personal.
It is deliberately not the default.

## What is still not logged

Conversation ids, tenant ids, request ids, timings, status, trace events, token counts
and costs are logged. Assistant answers are stored in the conversation store and
archived to object storage under retention; they are not written to the application log.

## Response headers

The API sets `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
`Referrer-Policy: no-referrer` and a restrictive `Permissions-Policy` on every
response. No CSP is set: the trace viewer is a single file with inline script, and a
policy that silently broke it would be worse than its explicit absence. A deployment
that serves the viewer behind an ingress should add its own CSP there.
