# Oracle — legacy constructs to identify

Matches the intake demo's Oracle→Databricks migration framing.

## Dialect functions & idioms

| Construct | kind | Why it matters for migration |
|---|---|---|
| `(+)` outer-join operator | join | Oracle legacy outer-join syntax; rewrite as ANSI `LEFT/RIGHT JOIN`. |
| `DECODE(x, a, b, c, d, e)` | dialect_function | Oracle conditional; rewrite as `CASE`. |
| `NVL`, `NVL2`, `NULLIF` | dialect_function | `NVL`→`COALESCE`; `NVL2` needs `CASE`. |
| `ROWNUM` / `ROWID` | pseudo_column | `ROWNUM` filtering → `ROW_NUMBER()`/`LIMIT`; `ROWID` has no portable meaning. |
| `CONNECT BY ... START WITH` | hierarchy | Hierarchical query; rewrite as `WITH RECURSIVE`. |
| `TO_DATE/TO_CHAR(d, 'DD-MON-YYYY')` | format | Oracle format masks differ; re-express. |
| `SYSDATE`, `ADD_MONTHS`, `MONTHS_BETWEEN` | date | Map to `current_timestamp`/`add_months`/`datediff`. |
| `VARCHAR2`, `NUMBER(p,s)`, `DATE` (with time) | typing | `NUMBER` without precision is arbitrary; Oracle `DATE` carries a time component — flag timestamp-vs-date ambiguity. |
| `MERGE INTO ... USING` | upsert | Portable to Snowflake/Databricks `MERGE`. |
| `PL/SQL` blocks (`BEGIN ... END;`), packages, cursors | procedural | Procedural logic — the biggest reverse-engineering effort; capture the *intent*, not the cursor mechanics. |
| Sequences `seq.NEXTVAL` | sequence | Surrogate keys; targets use `IDENTITY`/`GENERATED`. |
| Optimizer hints `/*+ INDEX(...) */` | hint | Oracle hints — drop, note if they signal a performance-critical path. |

## Job / connection patterns

- **`cx_Oracle` / `oracledb` / SQL*Plus** (`@script.sql`, `SPOOL`) → extract SQL, drop
  SQL*Plus formatting commands (`SET PAGESIZE`, `COLUMN ... FORMAT`).
- **Schemas = users** in Oracle; a fully-qualified `SALES.ORDERS` names the schema.
- Flag any reliance on **implicit `''` = NULL** (Oracle treats empty string as NULL) —
  targets do not, which changes filter results.
