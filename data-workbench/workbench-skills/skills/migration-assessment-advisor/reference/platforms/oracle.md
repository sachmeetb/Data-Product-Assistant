# Oracle — migration source notes

- **Identifier quoting:** double quotes; unquoted identifiers fold to **UPPERCASE**.
- **SQLAlchemy dialect:** `oracle+oracledb` (driver `oracledb`, thin mode — no Oracle
  client needed). DBNAME = the **service name**.
- **Incremental cursors:** an `updated_at`/`modified` column or a monotonic sequence-
  backed PK. Oracle has no `AUTO_INCREMENT`; identity columns (12c+) or
  sequence+trigger patterns are the norm.
- **Type caveats (the big ones — raw landing):**
  - **`DATE` carries a time component to the second** — it is NOT date-only. Land it
    as a timestamp, not a date, or you silently drop the time (flag as ambiguous).
  - **`NUMBER` without precision is arbitrary-precision** — a target with a fixed max
    scale (Snowflake 38, most warehouses) caps it (lossy). `NUMBER(p,s)` is exact.
  - `FLOAT` is NUMBER-based binary precision, not IEEE — distinct from
    `BINARY_FLOAT`/`BINARY_DOUBLE` (true IEEE).
  - `VARCHAR2` is the string type; `VARCHAR` is reserved/legacy. `LONG`/`LONG RAW`
    are deprecated (migrate to CLOB/BLOB). `ROWID`/`UROWID` are physical addresses —
    not portable.
  - Native `BOOLEAN` only in 23c+; older schemas encode it as `NUMBER(1)`/`CHAR(1)`.
- **Reconciliation checksum:** `STANDARD_HASH(col, 'MD5')` or `ORA_HASH(...)`.
- **Gotchas:** empty string `''` IS NULL in Oracle — count/compare with care during
  reconciliation. Constraints may be `DEFERRABLE`/`NOVALIDATE` — treat as informational.
