"""Memory data models: turns, entity facts and hypotheses.

Hypotheses are updated in place on contradiction rather than appended, so a conversation
can never hold "deploy ruled out" and "deploy confirmed" as two active facts.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

HYPOTHESIS_STATUSES = ("open", "confirmed", "ruled_out")


@dataclass(slots=True)
class Turn:
    user: str
    assistant: str
    timestamp: str | None = None


@dataclass(slots=True)
class Hypothesis:
    text: str
    status: str = "open"
    evidence: str = ""
    history: list[dict] = field(default_factory=list)


@dataclass(slots=True)
class Action:
    action: str
    outcome: str = ""
    timestamp: str = ""


@dataclass
class EntityMemory:
    conversation_id: str | None = None
    incident_id: str | None = None
    services: list[str] = field(default_factory=list)
    error_codes: list[str] = field(default_factory=list)
    hypotheses: list[Hypothesis] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    timeline_anchors: list[str] = field(default_factory=list)

    # ---- mutation ----

    def upsert_hypothesis(
        self, text: str, *, status: str = "open", evidence: str = ""
    ) -> Hypothesis:
        if status not in HYPOTHESIS_STATUSES:
            raise ValueError(f"unknown hypothesis status: {status}")
        key = _normalize(text)
        if not key:
            raise ValueError("hypothesis text must contain something other than punctuation")

        for hypothesis in self.hypotheses:
            if _normalize(hypothesis.text) != key:
                continue
            if hypothesis.status != status:
                hypothesis.history.append(
                    {"from": hypothesis.status, "to": status, "evidence": evidence}
                )
                hypothesis.status = status
            if evidence:
                hypothesis.evidence = evidence
            return hypothesis

        hypothesis = Hypothesis(text=text.strip(), status=status, evidence=evidence)
        self.hypotheses.append(hypothesis)
        return hypothesis

    def note_services(self, services: list[str]) -> None:
        _extend_unique(self.services, services)

    def note_error_codes(self, codes: list[str]) -> None:
        _extend_unique(self.error_codes, codes)

    def add_action(self, action: str, outcome: str = "", timestamp: str = "") -> None:
        entry = Action(action=action.strip(), outcome=outcome.strip(), timestamp=timestamp)
        if all(asdict(existing) != asdict(entry) for existing in self.actions):
            self.actions.append(entry)

    def add_timeline_anchor(self, anchor: str) -> None:
        _extend_unique(self.timeline_anchors, [anchor])

    # ---- views ----

    def essentials(self) -> EntityMemory:
        """Trim to the facts that must never be lost: services + confirmed hypotheses."""
        return EntityMemory(
            conversation_id=self.conversation_id,
            incident_id=self.incident_id,
            services=list(self.services),
            error_codes=list(self.error_codes),
            hypotheses=[
                Hypothesis(
                    text=h.text,
                    status=h.status,
                    evidence=h.evidence,
                    history=list(h.history),
                )
                for h in self.hypotheses
                if h.status == "confirmed"
            ],
        )

    def render(self) -> str:
        lines: list[str] = []
        if self.incident_id:
            lines.append(f"INCIDENT: {self.incident_id}")
        if self.services:
            lines.append("SERVICES: " + ", ".join(self.services))
        if self.error_codes:
            lines.append("ERROR CODES: " + ", ".join(self.error_codes))
        if self.hypotheses:
            lines.append("HYPOTHESES:")
            for hypothesis in self.hypotheses:
                detail = f"- [{hypothesis.status}] {hypothesis.text}"
                if hypothesis.evidence:
                    detail += f" (evidence: {hypothesis.evidence})"
                lines.append(detail)
        if self.actions:
            lines.append("ACTIONS:")
            for action in self.actions:
                detail = f"- {action.action}"
                if action.outcome:
                    detail += f" -> {action.outcome}"
                if action.timestamp:
                    detail += f" ({action.timestamp})"
                lines.append(detail)
        if self.timeline_anchors:
            lines.append("TIMELINE:")
            lines.extend(f"- {anchor}" for anchor in self.timeline_anchors)
        return "\n".join(lines)

    # ---- serialisation ----

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> EntityMemory:
        return cls(
            conversation_id=data.get("conversation_id"),
            incident_id=data.get("incident_id"),
            services=list(data.get("services") or []),
            error_codes=list(data.get("error_codes") or []),
            hypotheses=[
                Hypothesis(
                    text=item.get("text", ""),
                    status=item.get("status", "open"),
                    evidence=item.get("evidence", ""),
                    history=list(item.get("history") or []),
                )
                for item in data.get("hypotheses") or []
            ],
            actions=[
                Action(
                    action=item.get("action", ""),
                    outcome=item.get("outcome", ""),
                    timestamp=item.get("timestamp", ""),
                )
                for item in data.get("actions") or []
            ],
            timeline_anchors=list(data.get("timeline_anchors") or []),
        )


def _normalize(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9_]+", (text or "").lower()))


def _extend_unique(target: list[str], values: list[str]) -> None:
    seen = {value.lower() for value in target}
    for value in values:
        cleaned = (value or "").strip()
        if cleaned and cleaned.lower() not in seen:
            target.append(cleaned)
            seen.add(cleaned.lower())
