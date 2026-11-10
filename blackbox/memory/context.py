"""Budgeted context assembly with the documented eviction order.

Injection order is entity memory -> summary -> recent turns -> retrieved chunks ->
question. The system prompt is never evicted; the output headroom is reserved and never
part of the input context.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from blackbox.config.settings import BudgetConfig
from blackbox.memory.models import Turn
from blackbox.memory.tokens import count_tokens, truncate_to_tokens
from blackbox.memory.working import render_turns

EVICTION_ORDER = ("recent_turns", "retrieved_chunks", "summary", "entity")


@dataclass
class ContextBundle:
    system_prompt: str
    entity: str
    summary: str
    recent: list[Turn]
    chunks: list[dict]
    question: str
    report: dict = field(default_factory=dict)

    def render(self) -> str:
        blocks: list[str] = []
        if self.entity:
            blocks.append(f"ENTITY MEMORY:\n{self.entity}")
        if self.summary:
            blocks.append(f"INCIDENT SUMMARY:\n{self.summary}")
        if self.recent:
            blocks.append(f"RECENT TURNS:\n{render_turns(self.recent)}")
        return "\n\n".join(blocks)


def build_context(
    *,
    system_prompt: str,
    memory,
    chunks: list[dict],
    question: str,
    budget: BudgetConfig,
) -> ContextBundle:
    entity_text = memory.entity.render()
    summary_text = memory.summary.text or ""
    recent = list(memory.working.recent())
    chunks = [dict(chunk) for chunk in chunks]

    requested = {
        "system": count_tokens(system_prompt),
        "entity": count_tokens(entity_text),
        "summary": count_tokens(summary_text),
        "recent_turns": _recent_tokens(recent),
        "retrieved_chunks": _chunks_tokens(chunks),
    }
    allocations = {
        "system": budget.system_prompt_tokens,
        "entity": budget.entity_memory_tokens,
        "summary": budget.summary_tokens,
        "recent_turns": budget.recent_turns_tokens,
        "retrieved_chunks": budget.retrieved_chunks_tokens,
    }

    trimmed = {"recent_turns": 0, "retrieved_chunks": 0, "summary": False, "entity": False}

    system_prompt = truncate_to_tokens(system_prompt, allocations["system"])

    if requested["entity"] > allocations["entity"]:
        entity_text = _fit_entity(memory.entity, entity_text, allocations["entity"])
        trimmed["entity"] = True

    if requested["summary"] > allocations["summary"]:
        summary_text = truncate_to_tokens(summary_text, allocations["summary"])
        trimmed["summary"] = True

    recent, dropped_turns = _fit_recent(recent, allocations["recent_turns"])
    trimmed["recent_turns"] = dropped_turns

    chunks, dropped_chunks = _fit_chunks(chunks, allocations["retrieved_chunks"])
    trimmed["retrieved_chunks"] = dropped_chunks

    evictions: list[dict] = []
    entity_reduced = False

    def total() -> int:
        return (
            count_tokens(system_prompt)
            + count_tokens(entity_text)
            + count_tokens(summary_text)
            + _recent_tokens(recent)
            + _chunks_tokens(chunks)
            + count_tokens(question)
        )

    while total() > budget.total_cap_tokens:
        changed = False
        for bucket in EVICTION_ORDER:
            if bucket == "recent_turns" and recent:
                recent.pop(0)
                evictions.append({"bucket": bucket, "reason": "oldest_turn"})
                changed = True
                break
            if bucket == "retrieved_chunks" and chunks:
                chunks.pop()
                evictions.append({"bucket": bucket, "reason": "lowest_score"})
                changed = True
                break
            if bucket == "summary" and summary_text:
                summary_text = _halve(summary_text)
                evictions.append({"bucket": bucket, "reason": "compress_further"})
                changed = True
                break
            if bucket == "entity" and entity_text:
                if not entity_reduced:
                    entity_text = memory.entity.essentials().render()
                    entity_reduced = True
                else:
                    entity_text = _halve(entity_text)
                evictions.append({"bucket": bucket, "reason": "trim_to_essentials"})
                changed = True
                break
        if not changed:
            break

    actual = {
        "system": count_tokens(system_prompt),
        "entity": count_tokens(entity_text),
        "summary": count_tokens(summary_text),
        "recent_turns": _recent_tokens(recent),
        "retrieved_chunks": _chunks_tokens(chunks),
    }
    report = {
        "buckets": {
            name: {"requested": requested[name], "allocated": actual[name]}
            for name in requested
        },
        "question_tokens": count_tokens(question),
        "total_tokens": total(),
        "total_cap": budget.total_cap_tokens,
        "output_headroom": budget.output_headroom_tokens,
        "trimmed": trimmed,
        "evictions": evictions,
        "over_cap": total() > budget.total_cap_tokens,
    }
    return ContextBundle(
        system_prompt=system_prompt,
        entity=entity_text,
        summary=summary_text,
        recent=recent,
        chunks=chunks,
        question=question,
        report=report,
    )


def _fit_recent(turns: list[Turn], limit: int) -> tuple[list[Turn], int]:
    kept: list[Turn] = []
    used = 0
    for turn in reversed(turns):
        size = count_tokens(render_turns([turn]))
        if kept and used + size > limit:
            break
        kept.append(turn)
        used += size
    kept.reverse()
    return kept, len(turns) - len(kept)


def _fit_chunks(chunks: list[dict], limit: int) -> tuple[list[dict], int]:
    kept: list[dict] = []
    used = 0
    for chunk in chunks:
        size = count_tokens(chunk.get("text", ""))
        if kept and used + size > limit:
            break
        kept.append(chunk)
        used += size
    return kept, len(chunks) - len(kept)


def _fit_entity(entity, rendered: str, limit: int) -> str:
    essentials = entity.essentials().render()
    if count_tokens(essentials) <= limit:
        return essentials
    return truncate_to_tokens(essentials, limit)


def _halve(text: str) -> str:
    return truncate_to_tokens(text, max(0, count_tokens(text) // 2))


def _recent_tokens(turns: list[Turn]) -> int:
    return count_tokens(render_turns(turns)) if turns else 0


def _chunks_tokens(chunks: list[dict]) -> int:
    return sum(count_tokens(chunk.get("text", "")) for chunk in chunks)
