# Snowflake — migration target notes

- **Identifier quoting:** double quotes; **unquoted identifiers fold to UPPERCASE**
  (the opposite of Postgres) — a source `employees` lands as `EMPLOYEES`. Note this
  when reconciling by name.
- **dlt destination:** `dlt[snowflake]`; credentials need account, user, password,
  database, warehouse, (optional) role. A running/available warehouse is required.
- **Type caveats (target):** all floats are 64-bit (`FLOAT`/`DOUBLE` collapse).
  `NUMBER(p,s)` max precision 38 — a wider source numeric is capped (lossy).
  Semi-structured → `VARIANT`. `TIMESTAMP_NTZ` vs `TIMESTAMP_TZ` — pick per source
  column's zone semantics; a naive source timestamp should land `TIMESTAMP_NTZ`.
  No native fixed-width CHAR semantics (trailing spaces not padded).
- **Reconciliation checksum:** `HASH(...)` / `MD5(...)`.
- **Gotchas:** case-folding + the warehouse requirement are the two most common
  first-run failures.
