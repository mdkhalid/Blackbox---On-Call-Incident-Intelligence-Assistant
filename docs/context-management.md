# Context Management — Blackbox

## Three Memory Layers

### 1. Working Memory
- Last N turns (config, default 6) verbatim, including user + assistant messages.
- Stored in conversation state; persisted to Postgres.
- Bounded by token budget; oldest evicted first when over budget.

### 2. Summary Memory
- Older turns compressed into an incident-aware summary.
- **Trigger policy**: compress when working memory exceeds threshold (e.g. > 8 turns or > 1200 tokens). Run compression in background or inline before next turn.
- **Compression prompt** explicitly instructs preservation of:
  - Ruled-out hypotheses (with reason)
  - Confirmed facts
  - Actions already taken (with outcome)
  - Timeline anchors
  - Services/error codes implicated
- **Deliberately dropped**: conversational filler, repeated questions, abandoned tangents, pleasantries.
- Summary is cumulative and updated (not replaced) each compression cycle; max length capped.

### 3. Entity Memory
Structured facts accumulated during the conversation:
```json
{
  "incident_id": "INC-2024-0315",
  "services_implicated": ["checkout-service", "payment-gateway"],
  "hypotheses": [
    {"text": "DB connection pool exhaustion", "status": "confirmed", "evidence": "pm-003 root cause"},
    {"text": "01:15 deploy caused regression", "status": "ruled_out", "evidence": "deploy succeeded, no code change to pool"}
  ],
  "actions_taken": [{"action": "checked pool metrics", "timestamp": "...", "outcome": "saturated"}],
  "timeline_anchors": ["01:15 deploy", "01:32 first alert"]
}
```
- Queried and injected on every turn, separate from conversation history.
- Persisted to Postgres per conversation.

## Conflict Resolution
- If engineer says "actually the deploy was the cause" after ruled out: update hypothesis status to `confirmed`, record previous status in `history` field, log conflict event. Never hold both contradictory states as active.
- Implementation: upsert by hypothesis text (normalized) with status overwrite + audit entry.

## Token Budgeting
Allocation (config, tokens):
| Bucket | Default |
|---|---|
| System prompt | 800 |
| Entity memory | 400 |
| Summary | 600 |
| Recent turns | 1200 |
| Retrieved chunks | 2000 |
| Output headroom | 1024 |
| **Total cap** | **6024** |

- Measured with tiktoken (or provider counter).
- Exposed via `GET /api/conversations/:id/budget` (last turn's allocation).

## Eviction Order (when over budget)
1. Oldest recent turns (drop from working memory, fold into summary).
2. Lowest-score retrieved chunks.
3. Compress summary further (re-run compression with tighter target).
4. Trim entity memory to essentials (services + confirmed hypotheses only).
5. Never drop system prompt.

Justified: recent turns least critical after compression; chunks re-retrievable; summary compacted; entity memory smallest but most valuable, last to go.

## Injection Order in Prompt
System prompt → entity memory → summary → recent turns → retrieved chunks → user question. (Entity before summary so structured facts anchor the model.)

## Long Conversation Coherence
- Demo: 40+ turn conversation; verify ruled-out hypothesis survives compression; verify eviction triggers without breaking coherence.
- Track in eval/demo video.

## Context Retrieval (entity lookup)
- Router may route to entity/graph lookup (Postgres) for questions like "which services depend on payment gateway?" — answered from structured store, not embeddings.
- Result injected as context.

## Persistence
- Conversations, turns, entity memory, summaries persisted to Postgres.
- Redis caches hot conversation state for low-latency turns; DB is source of truth.
