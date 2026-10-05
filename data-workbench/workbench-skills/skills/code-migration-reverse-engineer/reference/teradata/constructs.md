# Teradata — legacy constructs to identify

Teradata is niche enough that a general LLM often lacks reliable knowledge — ground
strictly on this corpus. This is the platform from the code-migration demo
(Teradata → Databricks).

## Dialect functions & idioms

| Construct | kind | Why it matters for migration |
|---|---|---|
| `QUALIFY ROW_NUMBER() OVER (...)` | dialect | Teradata pioneered `QUALIFY`; Databricks/Snowflake support it, but plain Spark SQL <3.4 needs a subquery. |
| `SEL` (abbrev. of `SELECT`), `.LOGON`, `.LOGOFF` | bteq | BTEQ script commands, not SQL — strip the BTEQ wrapper and extract the SQL. |
| `CAST(x AS FORMAT '...')` | format | Teradata `FORMAT` phrases have no target equivalent — re-express via `to_char`-style. |
| `SUBSTR`, `INDEX(str, sub)`, `OTRANSLATE` | dialect_function | `INDEX`→`instr`/`position`; `OTRANSLATE`→`translate`. |
| `ADD_MONTHS`, `td_day_of_week` | date | `td_*` calendar functions are Teradata-only — decompose into portable date math. |
| `SAMPLE n` | sampling | Row sampling clause; targets use `TABLESAMPLE`/`LIMIT`. |
| Multiset vs SET tables | ddl | SET tables silently dedupe rows — a load target may differ; flag it. |
| `COLLECT STATISTICS` | maintenance | Optimizer stats — drop on migration (target manages its own). |
| Primary Index `PRIMARY INDEX (col)` | ddl | Distribution key, not a PK — do NOT treat as a uniqueness constraint; maps to Databricks `CLUSTER BY` / Snowflake clustering (advisory). |
| `HELP`, `SHOW TABLE`, `EXPLAIN` | metadata | Teradata metadata commands — not portable, drop. |

## Job / connection patterns

- **BTEQ / TPT / FastLoad / MLoad** scripts wrap SQL in a load harness. Reverse-engineer
  the SQL logic; the harness itself is replaced by the target's ingestion.
- **Teradata utilities in shell/Python** (`teradatasql`, `.run file=...`) → record the
  referenced databases (Teradata "databases" are schemas).
- Watch for **implicit case-insensitive comparisons** (`NOT CASESPECIFIC`) — Databricks
  is case-sensitive on string compares by default; flag as a semantics risk.
