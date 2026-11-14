"""Tenant move between regions."""
from __future__ import annotations

from blackbox.api.store import SqlAlchemyConversationRepository
from blackbox.ops.drills import move_tenant


def _seed(repo: SqlAlchemyConversationRepository, tenant_id: str) -> None:
    for index in range(2):
        conversation_id = f"conv-{index}"
        repo.get_or_create(conversation_id, tenant_id=tenant_id)
        repo.append_turn(
            conversation_id, "q", "a", "answered", [], {}, tenant_id=tenant_id
        )


def test_move_tenant_copies_rows_and_flips_home_region(tmp_path):
    source_url = f"sqlite:///{tmp_path / 'eu.db'}"
    target_url = f"sqlite:///{tmp_path / 'us.db'}"
    source = SqlAlchemyConversationRepository(source_url)
    source.init()
    _seed(source, "tenant-a")
    source.set_home_region("tenant-a", "eu-west-1")

    result = move_tenant(
        source_url=source_url,
        target_url=target_url,
        tenant_id="tenant-a",
        target_region="us-east-1",
    )

    assert result["verified"] is True
    assert result["status"] == "active"
    assert result["conversations_exported"] == 2
    assert result["turns_exported"] == 2

    target = SqlAlchemyConversationRepository(target_url)
    assert len(target.turns_for_tenant("tenant-a")) == 2
    assert target.home_region("tenant-a") == "us-east-1"
    assert target.tenant_region_status("tenant-a") == "active"


def test_move_is_idempotent(tmp_path):
    source_url = f"sqlite:///{tmp_path / 'eu.db'}"
    target_url = f"sqlite:///{tmp_path / 'us.db'}"
    source = SqlAlchemyConversationRepository(source_url)
    source.init()
    _seed(source, "tenant-b")

    first = move_tenant(
        source_url=source_url, target_url=target_url,
        tenant_id="tenant-b", target_region="us-east-1",
    )
    second = move_tenant(
        source_url=source_url, target_url=target_url,
        tenant_id="tenant-b", target_region="us-east-1",
    )

    assert first["verified"] and second["verified"]
    # A retried move does not duplicate the conversations.
    assert second["conversations_imported"] == 0
    target = SqlAlchemyConversationRepository(target_url)
    assert len(target.turns_for_tenant("tenant-b")) == 2
