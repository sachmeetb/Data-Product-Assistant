#!/usr/bin/env bash
# Idempotent multi-sample loader for the CLI-managed MySQL container.
#
# Run INSIDE the mysql container by `dwb up` (via `docker compose exec`) after the
# service is healthy. Each MySQL sample's SQL creates its own schema(s) with
# `CREATE SCHEMA IF NOT EXISTS`, so we only guard at the sample level: if the
# sample's primary schema already exists, skip it. Re-running `dwb up` is safe.
#
#   mysql-load.sh <name:primary_schema> [<name:primary_schema> ...]
#
# SQL lives at /samples/<name>/*.sql (repo `samples/` mounted read-only at
# /samples). MYSQL_ROOT_PASSWORD is provided by the container env.
set -euo pipefail

if [ "$#" -eq 0 ]; then
  echo "mysql-load.sh: no samples requested" >&2
  exit 0
fi

mysql_root() { mysql -uroot -p"$MYSQL_ROOT_PASSWORD" "$@"; }

for tok in "$@"; do
  case "$tok" in
    *:*) ;;
    *) echo "mysql-load.sh: malformed token '$tok' (want name:schema)" >&2; exit 1 ;;
  esac
  name="${tok%%:*}"
  schema="${tok##*:}"
  if [ -z "$name" ] || [ -z "$schema" ]; then
    echo "mysql-load.sh: malformed token '$tok' (want name:schema)" >&2
    exit 1
  fi

  exists=$(mysql_root -N -B -e \
    "SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name='$schema'")
  if [ "$exists" != "0" ]; then
    echo "skip $name (schema '$schema' already exists)"
    continue
  fi

  echo "loading sample '$name' (primary schema '$schema')"
  for f in $(ls "/samples/$name"/*.sql 2>/dev/null | sort); do
    echo "  loading $(basename "$f")"
    mysql_root < "$f"
  done
  echo "loaded $name"
done
