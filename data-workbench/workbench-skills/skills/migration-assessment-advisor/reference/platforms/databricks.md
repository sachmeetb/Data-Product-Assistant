# Databricks — migration target notes

- **Identifier quoting:** backticks. Three-level namespace: `catalog.schema.table`
  (Unity Catalog).
- **dlt destination:** `dlt[databricks]`; credentials need server hostname,
  http_path (SQL warehouse), access token, and a catalog.
- **Type caveats (target):** `FLOAT` is 32-bit, `DOUBLE` is 64-bit (distinct — a
  64-bit source float must land `DOUBLE`). `TIMESTAMP` carries a session-local zone
  (tz-ambiguity — flag naive source timestamps); `TIMESTAMP_NTZ` is the naive
  variant. No native `TIME` type (a time-only source column has no clean target —
  land as string or timestamp; flag as lossy). `DECIMAL` max precision 38.
- **Reconciliation checksum:** `md5(...)` / `hash(...)`.
- **Gotchas:** the target catalog must exist and be writable; the SQL warehouse must
  be running. Set `WB_TARGET_CATALOG` (and schema) explicitly.
