# Postgres — migration source/target notes

- **Identifier quoting:** double quotes; identifiers fold to lowercase unless quoted.
- **SQLAlchemy dialect:** `postgresql+psycopg2` (driver `psycopg2-binary`).
- **Incremental cursors:** `updated_at`/`modified` timestamps are reliable; serial/
  `bigserial` PKs are monotonic. `xmin` is a system column, not a stable cursor.
- **Type caveats (raw landing):** `NUMERIC` without precision is arbitrary
  precision — target platforms with a fixed max scale may cap it (flag as lossy).
  `JSONB`/`JSON` → target `json`/`variant`. `UUID` often lands as string on
  warehouses (ambiguous). `timestamptz` carries a zone; a target with only naive
  timestamps loses it. Arrays and composite types rarely have a clean target
  equivalent — flag as unsupported.
- **Reconciliation checksum:** `md5(...)`.
- **Gotchas:** views vs tables — migrate base tables, not views, unless the view is
  the intended deliverable.
