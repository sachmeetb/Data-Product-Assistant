#!/usr/bin/env bash
# Idempotent multi-sample loader for the CLI-managed Postgres container.
#
# Run INSIDE the postgres container by `dwb up` (via `docker compose exec`) after
# the service is healthy. Creates one database per selected sample and loads its
# SQL fixtures in order. A sample whose database already exists is skipped, so
# re-running `dwb up` is safe and new samples can be added without a volume wipe.
#
#   pg-load.sh <name:db> [<name:db> ...]
#
# Each sample's SQL lives at /samples/<name>/*.sql (the repo `samples/` dir is
# mounted read-only at /samples). Files run in lexical order (01_schema ->
# 02_seed -> 03_consumer_schema). POSTGRES_USER is provided by the container env.
set -euo pipefail

if [ "$#" -eq 0 ]; then
  echo "pg-load.sh: no samples requested" >&2
  exit 0
fi

for tok in "$@"; do
  case "$tok" in
    *:*) ;;
    *) echo "pg-load.sh: malformed token '$tok' (want name:db)" >&2; exit 1 ;;
  esac
  name="${tok%%:*}"
  db="${tok##*:}"
  if [ -z "$name" ] || [ -z "$db" ]; then
    echo "pg-load.sh: malformed token '$tok' (want name:db)" >&2
    exit 1
  fi

  # Probe against the always-present `postgres` maintenance DB — without -d, psql
  # would connect to a DB named after POSTGRES_USER (which may not exist), the
  # probe would error, and the guard would wrongly fall through to createdb.
  if psql -U "$POSTGRES_USER" -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$db'" | grep -q 1; then
    echo "skip $name (database '$db' already exists)"
    continue
  fi

  echo "creating database '$db' for sample '$name'"
  createdb -U "$POSTGRES_USER" "$db"

  for f in $(ls "/samples/$name"/*.sql 2>/dev/null | sort); do
    echo "  loading $(basename "$f")"
    psql -U "$POSTGRES_USER" -d "$db" -v ON_ERROR_STOP=1 -q -f "$f"
  done
  echo "loaded $name -> $db"
done
