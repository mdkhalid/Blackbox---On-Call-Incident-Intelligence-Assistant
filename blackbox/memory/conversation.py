"""Conversation memory: the three layers plus the compression trigger policy."""
from __future__ import annotations

from dataclasses import dataclass, field

from blackbox.config.settings import MemoryConfig
from blackbox.llm.base import LLMProvider
from blackbox.memory.models import EntityMemory, Turn
from blackbox.memory.prompts import SUMMARY_SYSTEM, summary_prompt
from blackbox.memory.summary import SummaryMemory
from blackbox.memory.tokens import truncate_to_tokens
from blackbox.memory.working import WorkingMemory


@dataclass
class ConversationMemory:
    config: MemoryConfig = field(default_factory=MemoryConfig)
    entity: EntityMemory = field(default_factory=EntityMemory)
    summary: SummaryMemory = field(default_factory=SummaryMemory)
    working: WorkingMemory = field(init=False)
    last_budget: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.working = WorkingMemory(max_turns=self.config.working_turns)

    def add_turn(
        self, user: str, assistant: str, timestamp: str | None = None
    ) -> Turn:
        turn = Turn(user=user, assistant=assistant, timestamp=timestamp)
        self.working.append(turn)
        self.summary.turns_since_compression += 1
        return turn

    @property
    def compression_due(self) -> bool:
        """True when there are more turns than working memory can keep verbatim."""
        return (
            self.summary.turns_since_compression >= self.config.summary_trigger_turns
            and len(self.working.turns) > self.working.max_turns
        )

    def maybe_compress(self, llm: LLMProvider, *, model: str | None = None) -> bool:
        """Fold the turns that no longer fit into the cumulative summary.

        Called inline before the next turn (or in the background by the API). Returns
        whether a compression actually happened.
        """
        if not self.compression_due:
            return False

        fold_count = len(self.working.turns) - self.working.max_turns
        older = self.working.turns[:fold_count]
        response = llm.complete(
            system=SUMMARY_SYSTEM,
            prompt=summary_prompt(self.summary.text, older),
            model=model,
            temperature=0.2,
            max_tokens=self.config.summary_max_tokens,
        )
        self.summary.text = truncate_to_tokens(
            (response.text or "").strip(), self.config.summary_max_tokens
        )
        self.summary.turns_since_compression = 0
        self.summary.compression_count += 1
        self.working.drop(fold_count)
        return True

    def to_dict(self) -> dict:
        return {
            "config": self.config.model_dump(),
            "entity": self.entity.to_dict(),
            "summary": {
                "text": self.summary.text,
                "turns_since_compression": self.summary.turns_since_compression,
                "compression_count": self.summary.compression_count,
            },
            "working": [
                {"user": turn.user, "assistant": turn.assistant, "timestamp": turn.timestamp}
                for turn in self.working.turns
            ],
            "last_budget": self.last_budget,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ConversationMemory:
        memory = cls(config=MemoryConfig(**(data.get("config") or {})))
        memory.entity = EntityMemory.from_dict(data.get("entity") or {})
        summary = data.get("summary") or {}
        memory.summary = SummaryMemory(
            text=summary.get("text", ""),
            turns_since_compression=int(summary.get("turns_since_compression", 0)),
            compression_count=int(summary.get("compression_count", 0)),
        )
        memory.working.turns = [
            Turn(
                user=item.get("user", ""),
                assistant=item.get("assistant", ""),
                timestamp=item.get("timestamp"),
            )
            for item in data.get("working") or []
        ]
        memory.last_budget = dict(data.get("last_budget") or {})
        return memory
