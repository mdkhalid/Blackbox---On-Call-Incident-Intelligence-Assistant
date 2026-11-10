"""Working memory: the last N turns verbatim."""
from __future__ import annotations

from dataclasses import dataclass, field

from blackbox.memory.models import Turn


@dataclass
class WorkingMemory:
    max_turns: int = 6
    turns: list[Turn] = field(default_factory=list)

    def append(self, turn: Turn) -> None:
        self.turns.append(turn)

    def recent(self) -> list[Turn]:
        if self.max_turns <= 0:
            return []
        return list(self.turns[-self.max_turns :])

    def overflow(self) -> list[Turn]:
        if self.max_turns <= 0:
            return list(self.turns)
        return list(self.turns[: -self.max_turns]) if len(self.turns) > self.max_turns else []

    def drop(self, count: int) -> None:
        if count > 0:
            del self.turns[:count]

    def render(self, turns: list[Turn] | None = None) -> str:
        selected = self.recent() if turns is None else turns
        return render_turns(selected)


def render_turns(turns: list[Turn]) -> str:
    return "\n".join(
        f"USER: {turn.user}\nASSISTANT: {turn.assistant}" for turn in turns
    )
