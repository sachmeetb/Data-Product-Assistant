# DLT write dispositions

- **`replace`** (default for raw lift-and-shift) — the target table is overwritten
  with the source snapshot each run. Idempotent; no primary key required.
- **`append`** — new rows are added to the target. Combine with an incremental
  cursor (`dlt.sources.incremental` on `updated_at` / a monotonic PK) to load only
  new/changed rows and avoid re-reading the whole table.
- **`merge`** — upsert on a primary key. Requires the resource to declare a primary
  key (`resource.apply_hints(primary_key=[...])`). Use only when the target must
  reflect updates in place.

Raw migration Phase 1 uses `replace` (full snapshot) or `append`. CDC (true
change-data-capture) is delegated to an external provider (Debezium / DMS), NOT
dlt — dlt is the snapshot + cursor-incremental mover.

Schema evolution: dlt infers + evolves the target schema from the source by
default. For a strict migration set the plan's `schema_change_policy` to `fail` if
you want drift to stop the run.
