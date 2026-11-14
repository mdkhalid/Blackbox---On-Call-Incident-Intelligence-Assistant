#!/bin/sh
# Bootstrap the streaming replica on first start, then run Postgres in standby mode.
set -eu

PGDATA="${PGDATA:-/var/lib/postgresql/data/pgdata}"

if [ ! -s "$PGDATA/PG_VERSION" ]; then
  echo "replica: base backup from ${PRIMARY_HOST}"
  PGPASSWORD="${POSTGRES_PASSWORD}" pg_basebackup \
    -h "${PRIMARY_HOST}" \
    -U "${POSTGRES_USER}" \
    -D "$PGDATA" \
    -Fp -Xs -P -R
  chmod 700 "$PGDATA"
fi

exec postgres -D "$PGDATA"
