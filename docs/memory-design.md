# Memory Design — Blackbox

See `docs/context-management.md` for full details. Summary below.

## Three Layers
1. **Working memory**: last N turns verbatim (default 6).
2. **Summary memory**: incident-aware compression of older turns. Preserves ruled-out hypotheses, confirmed facts, actions taken, timeline anchors. Drops filler.
3. **Entity memory**: structured facts (incident_id, services_implicated, hypotheses with status, actions_taken, timeline_anchors). Injected every turn separately from history.

## Conflict Resolution
On contradiction (e.g., "deploy was actually the cause" after ruled out), update hypothesis status rather than hold both. Audit entry recorded.

## Token Budgeting
Explicit allocation across system/entity/summary/recent/chunks/output with reserved headroom. Eviction order defined and demonstrated. Exposed via `/api/conversations/:id/budget`.

## Trigger Policy
Compress when working memory exceeds threshold (turns or tokens). Cumulative summary updated each cycle.

## What Compression Preserves vs Drops
- Preserves: ruled-out hypotheses (+reason), confirmed facts, actions (+outcome), timeline anchors, services/error codes.
- Drops: filler, repeated questions, abandoned tangents, pleasantries.
