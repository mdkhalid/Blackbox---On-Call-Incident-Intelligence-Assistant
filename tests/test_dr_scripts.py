"""DR drill harness: failover and restore measurements."""
from __future__ import annotations

from blackbox.api.store import SqlAlchemyConversationRepository
from blackbox.ops.drills import make_backup, measure_restore, simulate_failover


def test_failover_measures_rto_and_rpo(tmp_path):
    source = f"sqlite:///{tmp_path / 'primary.db'}"
    target = f"sqlite:///{tmp_path / 'replica.db'}"

    result = simulate_failover(
        source_url=source, target_url=target, conversations=5, tenant_id="t1"
    )

    assert result.rto_s >= 0
    assert result.rpo_rows == 0
    assert result.rows_before == 5
    assert result.rows_after == 5

    # The promoted target serves the seeded data and accepts a canary write.
    repo = SqlAlchemyConversationRepository(target)
    assert len(repo.get_turns("drill-0", tenant_id="t1")) == 1
    assert repo.get("drill-post-failover", tenant_id="t1") is not None


def test_restore_measures_rto_and_rpo(tmp_path):
    source = f"sqlite:///{tmp_path / 'live.db'}"
    repo = SqlAlchemyConversationRepository(source)
    repo.init()
    repo.get_or_create("c1", tenant_id="t1")
    for index in range(3):
        repo.append_turn("c1", f"q{index}", "a", "answered", [], {}, tenant_id="t1")

    backup = tmp_path / "backup.db"
    make_backup(source, backup)
    result = measure_restore(
        source_url=source,
        backup_path=backup,
        restored_url=f"sqlite:///{tmp_path / 'restored.db'}",
        tenant_id="t1",
    )

    assert result.rpo_rows == 0
    assert result.rows_before == result.rows_after == 3
    assert result.rto_s >= 0


def test_failover_requires_a_promotion_path():
    try:
        simulate_failover(
            source_url="postgresql://primary/db",
            target_url="postgresql://replica/db",
            conversations=0,
        )
    except ValueError as exc:
        assert "promote callable" in str(exc)
    else:  # pragma: no cover - the guard must fire
        raise AssertionError("expected a ValueError")
