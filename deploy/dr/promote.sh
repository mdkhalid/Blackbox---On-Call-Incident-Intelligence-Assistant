#!/bin/sh
# Promote the streaming replica. This is the manual step a failover runbook performs;
# in a managed service it is the provider's failover call instead.
set -eu

CONTAINER="${1:-blackbox-dr-replica}"
docker exec -u postgres "$CONTAINER" pg_ctl promote -D /var/lib/postgresql/data/pgdata
