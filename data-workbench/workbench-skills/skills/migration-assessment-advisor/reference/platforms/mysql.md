# MySQL — migration source/target notes

- **Identifier quoting:** backticks.
- **SQLAlchemy dialect:** `mysql+pymysql` (driver `pymysql`).
- **Incremental cursors:** `updated_at` with `ON UPDATE CURRENT_TIMESTAMP` is ideal;
  `AUTO_INCREMENT` PKs are monotonic.
- **Type caveats (raw landing):** `UNSIGNED` integers can overflow a signed target
  type (flag as lossy — e.g. unsigned BIGINT → signed int64). `TINYINT(1)` is often
  a boolean — confirm intent. `ENUM`/`SET` have no clean target equivalent (lossy →
  land as string). `DATETIME` is timezone-naive; `TIMESTAMP` is UTC-normalized —
  don't conflate them. Zero dates (`0000-00-00`) are invalid on most targets.
- **Reconciliation checksum:** `MD5(...)`.
- **Gotchas:** default charset/collation differences can change string comparison on
  the target; note the source collation for text-heavy tables.
