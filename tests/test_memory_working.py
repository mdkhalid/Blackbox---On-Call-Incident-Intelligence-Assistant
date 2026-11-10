from blackbox.memory import Turn, WorkingMemory


def _turns(count: int) -> list[Turn]:
    return [Turn(user=f"user {i}", assistant=f"assistant {i}") for i in range(count)]


def test_recent_returns_the_last_n_turns():
    memory = WorkingMemory(max_turns=2)
    for turn in _turns(3):
        memory.append(turn)

    assert [turn.user for turn in memory.recent()] == ["user 1", "user 2"]


def test_overflow_returns_the_oldest_turns():
    memory = WorkingMemory(max_turns=2)
    for turn in _turns(3):
        memory.append(turn)

    assert [turn.user for turn in memory.overflow()] == ["user 0"]


def test_render_includes_both_roles():
    memory = WorkingMemory(max_turns=2)
    memory.append(Turn(user="check p99", assistant="it is spiking"))

    rendered = memory.render()
    assert "USER: check p99" in rendered
    assert "ASSISTANT: it is spiking" in rendered


def test_drop_removes_oldest_turns():
    memory = WorkingMemory(max_turns=2)
    for turn in _turns(3):
        memory.append(turn)

    memory.drop(1)
    assert [turn.user for turn in memory.turns] == ["user 1", "user 2"]
