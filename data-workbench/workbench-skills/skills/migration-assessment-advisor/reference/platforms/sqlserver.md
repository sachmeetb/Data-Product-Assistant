# SQL Server / Azure SQL — migration source notes

Covers on-prem **SQL Server** and **Azure SQL Database** (same engine).

- **Identifier quoting:** square brackets `[...]` (or double quotes with
  `QUOTED_IDENTIFIER ON`). Three-level namespace: `database.schema.table`.
- **SQLAlchemy dialect:** `mssql+pyodbc` (driver `pyodbc` + an ODBC driver, e.g.
  "ODBC Driver 18 for SQL Server"; Azure SQL requires encryption).
- **Incremental cursors:** an `updated_at` column, an `IDENTITY` PK (monotonic), or
  `rowversion` for change tracking (but see the gotcha below).
- **Type caveats (the big ones — raw landing):**
  - **`timestamp` / `rowversion` is an 8-byte auto-versioning BINARY, NOT a temporal
    type.** Never migrate it as a value — it should be regenerated on the target.
    (This is the #1 SQL Server migration trap.)
  - **`TINYINT` is unsigned 0–255** (not signed) — widen to a 16-bit target to avoid
    overflow (flag as lossy).
  - `DATETIME` rounds to ~3.33 ms; `DATETIME2` is full precision; `SMALLDATETIME` is
    minute precision. `DATETIMEOFFSET` carries a zone. Prefer landing everything as
    `DATETIME2`/timestamp.
  - `REAL` is 32-bit, `FLOAT(53)` is 64-bit. `MONEY`/`SMALLMONEY` → DECIMAL (currency
    semantics lost). `UNIQUEIDENTIFIER` (GUID) → string. `TEXT`/`NTEXT`/`IMAGE` are
    deprecated (migrate to (N)VARCHAR(MAX)/VARBINARY(MAX)). `SQL_VARIANT` is not
    portable.
- **Reconciliation checksum:** `HASHBYTES('MD5', ...)` (per-row) / `CHECKSUM_AGG(...)`.
- **Gotchas:** collation differences change string comparison + sort on the target;
  note the source collation for text-heavy tables. `bit` is 0/1 → boolean.
