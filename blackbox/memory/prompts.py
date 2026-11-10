"""Prompt for incident-aware summary compression.

The preservation list is explicit because a generic "summarise this" prompt silently drops
ruled-out hypotheses, which are the most valuable fact in an incident conversation.
"""
from __future__ import annotations

from blackbox.memory.models import Turn
from blackbox.memory.working import render_turns

SUMMARY_SYSTEM = (
    "You compress on-call incident conversations into a cumulative incident summary. "
    "You MUST preserve: ruled-out hypotheses and the reason each was ruled out, confirmed "
    "facts, actions already taken with their outcome, timeline anchors, and implicated "
    "services/error codes. Preserve identifiers verbatim. DROP: pleasantries, repeated "
    "questions, conversational filler and abandoned tangents. Return only the updated "
    "summary."
)


def summary_prompt(existing_summary: str, turns: list[Turn]) -> str:
    return (
        "TASK: SUMMARIZE\n"
        f"EXISTING_SUMMARY: {existing_summary or '(none)'}\n"
        "TURNS:\n"
        f"{render_turns(turns)}\n\n"
        "Return the updated cumulative incident summary."
    )
