# Tenant Move Runbook

Moving a tenant between regions. The order is deliberate: import, verify, then flip
ownership. A failure before the flip leaves the tenant served from its old home
region, which is the safe state.

## Procedure

1. **Destination ready.** The target region's database and retrieval indexes exist and
   pass `/health/ready`.
2. **Export from the source.**
   ```python
   exported = source_repo.export_tenant(tenant_id)
   ```
   This copies conversations, turns, and the current region mapping.
3. **Import into the destination.**
   ```python
   target_repo.import_tenant(exported, home_region=target_region)
   ```
   Import is idempotent per conversation and per turn id, so a retried move does not
   duplicate rows.
4. **Verify counts and a spot retrieval.** Compare `turns_for_tenant` before and after,
   and run one retrieval query for a document the tenant uses.
5. **Freeze writes for the tenant.** Mark `status='moving'` so a well-behaved client
   stops writing. Any write that lands in the source after export would be lost, so
   the freeze happens before step 3 in a real move.
6. **Flip ownership.** Set `tenant_regions.home_region = target_region` and
   `status='active'`.
7. **Drain and retire.** Delete the tenant's rows from the source region after the
   retention window, and clear the tenant's cache keys.

The automated version of steps 2–6 is `blackbox.ops.drills.move_tenant`:

```bash
python scripts/move_tenant.py --tenant acme --target-region us-east-1 \
  --source-url postgresql://.../eu --target-url postgresql://.../us
```

`verified: true` in the output means the destination has at least as many turns as
were exported.

## Retrieval indexes

Conversations move with the relational copy; vectors and search documents do not.
After the flip, ingest the tenant's documents into the destination region (or rebuild
the whole region's index with `scripts/rebuild_region_indexes.py`) before the tenant
reads from it.

## Rollback

Before step 6, rollback is a no-op: the source still owns the tenant. After step 6,
roll back by flipping `home_region` to the source, then re-importing any rows written
in the destination since the move.
