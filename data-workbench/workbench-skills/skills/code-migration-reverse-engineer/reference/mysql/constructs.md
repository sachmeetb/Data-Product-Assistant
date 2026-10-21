# MySQL — legacy constructs to identify

What to look for when reverse-engineering MySQL queries/jobs/reports. Flag each in
`identified_constructs` with its `kind` so the forward stage can choose a portable
target equivalent.

## Dialect functions & idioms

| Construct | kind | Why it matters for migration |
|---|---|---|
| `GROUP_CONCAT(expr SEPARATOR ',')` | dialect_function | String aggregation; targets use `array_join(collect_list(...))` (Spark) or `LISTAGG` (Snowflake). Watch the implicit `group_concat_max_len` truncation. |
| `IFNULL(a, b)` / `NULLIF` | dialect_function | `IFNULL` → `COALESCE`. Portable but often written the MySQL way. |
| `DATE_ADD(d, INTERVAL n DAY)` / `DATE_SUB` | dialect_function | Date math; targets use `date_add`/`dateadd` or `d + INTERVAL`. |
| `STR_TO_DATE` / `DATE_FORMAT(d, '%Y-%m-%d')` | dialect_function | `%`-style format tokens are MySQL-specific; must be re-expressed. |
| Backtick identifiers `` `col` `` | quoting | Backticks are MySQL-only; targets use double-quotes (ANSI) or their own rules. |
| `LIMIT n OFFSET m` | pagination | Portable to most targets but Snowflake prefers `LIMIT n OFFSET m` / `QUALIFY`. |
| `INSERT ... ON DUPLICATE KEY UPDATE` | upsert | No direct equivalent; targets use `MERGE` (Snowflake/Databricks). |
| `REPLACE INTO` | upsert | Delete+insert semantics; must become `MERGE` or explicit delete+insert. |
| `AUTO_INCREMENT` | ddl | Surrogate keys; targets use `IDENTITY`/`GENERATED` or sequences. |
| `TINYINT(1)` used as boolean | typing | Semantics lost on lift-and-shift; confirm whether it is a real boolean. |
| `ENGINE=InnoDB`, `CHARSET=utf8mb4` | ddl_noise | Storage-engine clauses — drop on migration, note collation if it affects ordering. |
| Implicit type coercion (`'5' + 1`) | semantics | MySQL silently coerces; targets are stricter. Flag any arithmetic on string columns. |
| `/*! ... */` optimizer hints | hint | MySQL executable comments — target-specific, usually dropped. |

## Connection / job patterns

- **Per-schema databases in one instance.** MySQL "schemas" are databases; a job that
  `USE hr_core; ... USE hr_comp;` is crossing databases on one server. There is often
  **no cross-schema FK** — joins ride a shared business key (e.g. `employee_id`).
- **`mysql.connector` / `PyMySQL` / SQLAlchemy `mysql+pymysql://`** connection strings
  in Python jobs → record the referenced schemas.
- **SCD-2 by `is_current` + `effective_from`/`effective_to`.** History tables keep a
  current-row flag; any report that filters `WHERE is_current = 1` encodes as-of logic
  that the target must preserve.
- **Cryptic column names** (`amt`, `rsn`, `lvl`): the meaning lives in the data, not the
  name — carry the reviewer note forward as a requirement, don't rename silently.
