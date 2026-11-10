"""Deterministic offline LLM.

This is not a stub that returns fixed strings: it reads the same prompt structure the real
providers receive and performs lexical grading, rewriting and extractive generation. That
keeps the whole graph runnable with no API key and makes the graph's control flow
(grade -> rewrite -> honest failure) genuinely testable rather than mocked away.
"""
from __future__ import annotations

import json
import re

from blackbox.llm.base import LLMResponse
from blackbox.text import content_tokens, split_sentences

_EVIDENCE_LINE = re.compile(r"^\[([^\]]+)\]\s+(.*)$")
_FIELD_LINE = re.compile(r"^(QUESTION|QUERY|ATTEMPT|EXISTING_SUMMARY):\s*(.*)$")
_SUMMARY_MARKERS = (
    "ruled out",
    "confirmed",
    "root cause",
    "checked",
    "action",
    "timeline",
    "deploy",
    "err_",
    "service",
    "p99",
    "latency",
    "incident",
    "because",
)


class FakeLLMProvider:
    name = "fake"

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 512,
        json_mode: bool = False,
    ) -> LLMResponse:
        text, task = self._respond(prompt, json_mode)
        response = LLMResponse(
            text=text,
            model=model or "fake",
            prompt_tokens=_count(f"{system} {prompt}"),
            completion_tokens=_count(text),
        )
        self.calls.append({"task": task, "prompt": prompt, "response": text})
        self.usage["prompt_tokens"] += response.prompt_tokens
        self.usage["completion_tokens"] += response.completion_tokens
        self.usage["total_tokens"] += response.total_tokens
        return response

    def _respond(self, prompt: str, json_mode: bool) -> tuple[str, str]:
        if "TASK: GRADE" in prompt:
            return self._grade(prompt), "grade"
        if "TASK: REWRITE" in prompt:
            return self._rewrite(prompt), "rewrite"
        if "TASK: SUMMARIZE" in prompt:
            return self._summarize(prompt), "summarize"
        if "TASK: GENERATE" in prompt:
            return self._generate(prompt), "generate"
        if json_mode:
            return "{}", "json"
        return "", "unknown"

    def _grade(self, prompt: str) -> str:
        query = _field(prompt, "QUERY") or _field(prompt, "QUESTION") or ""
        evidence = _evidence(prompt)
        query_terms = set(content_tokens(query))
        evidence_terms = set(content_tokens(" ".join(text for _, text in evidence)))

        overlap = query_terms & evidence_terms
        identifiers = {
            term
            for term in query_terms
            if "_" in term or term.endswith("service") or any(ch.isdigit() for ch in term)
        }
        sufficient = len(overlap) >= 2 or bool(identifiers & evidence_terms)
        reason = (
            f"evidence shares terms with the question: {sorted(overlap)}"
            if sufficient
            else "evidence shares no meaningful terms with the question"
        )
        return json.dumps({"sufficient": sufficient, "reason": reason})

    def _rewrite(self, prompt: str) -> str:
        query = _field(prompt, "QUESTION") or _field(prompt, "QUERY") or ""
        raw_tokens = re.findall(r"[A-Za-z0-9_]+", query)
        # Preserve the original spelling of identifiers: prompts promise to keep them
        # exactly, and a lowercase error code is still legible to BM25 but not to a human
        # reading the trace.
        identifier_case = {
            token.lower(): token
            for token in raw_tokens
            if "_" in token
            or any(ch.isdigit() for ch in token)
            or token.lower().endswith("service")
        }

        rendered: list[str] = []
        seen: set[str] = set()
        for term in content_tokens(query):
            display = identifier_case.get(term, term)
            if display.lower() in seen:
                continue
            seen.add(display.lower())
            rendered.append(display)
        for filler in ("incident", "postmortem"):
            if filler not in seen:
                rendered.append(filler)
                seen.add(filler)

        rewritten = " ".join(rendered).strip()
        if not rewritten or rewritten == query.strip():
            rewritten = f"{query.strip()} incident".strip()
        return rewritten

    def _generate(self, prompt: str) -> str:
        evidence = _evidence(prompt)
        if not evidence:
            return json.dumps({"answer": "", "claims": []})
        chunk_id, text = evidence[0]
        sentences = split_sentences(text)
        claim = sentences[0] if sentences else text
        return json.dumps(
            {
                "answer": f"{claim} [{chunk_id}]",
                "claims": [{"text": claim, "sources": [chunk_id]}],
            }
        )

    def _summarize(self, prompt: str) -> str:
        existing = _field(prompt, "EXISTING_SUMMARY") or ""
        turns_block = ""
        if "TURNS:" in prompt:
            turns_block = prompt.split("TURNS:", 1)[1].split("Return the updated", 1)[0]

        kept: list[str] = []
        if existing.strip() and existing.strip() != "(none)":
            kept.append(existing.strip())
        for line in turns_block.splitlines():
            for sentence in split_sentences(line.strip()) or [line.strip()]:
                if not sentence:
                    continue
                lowered = sentence.lower()
                if any(marker in lowered for marker in _SUMMARY_MARKERS):
                    entry = f"- {sentence}"
                    if entry not in kept:
                        kept.append(entry)
        return "\n".join(kept)


def _count(text: str) -> int:
    return len((text or "").split())


def _field(prompt: str, name: str) -> str | None:
    for line in prompt.splitlines():
        match = _FIELD_LINE.match(line.strip())
        if match and match.group(1) == name:
            return match.group(2)
    return None


def _evidence(prompt: str) -> list[tuple[str, str]]:
    lines = prompt.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == "EVIDENCE:")
    except StopIteration:
        return []
    out: list[tuple[str, str]] = []
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if stripped.startswith(("Return", "Answer", "QUESTION:", "QUERY:", "ATTEMPT:")):
            break
        match = _EVIDENCE_LINE.match(stripped)
        if match:
            out.append((match.group(1), match.group(2)))
        elif out and stripped:
            out[-1] = (out[-1][0], f"{out[-1][1]} {stripped}")
    return out
