# mysql-hr — legacy MySQL HR report

`legacy_headcount_report.sql` — a per-department headcount + compensation report
written against the **`hr-mysql`** sample (schemas `hr_core`, `hr_comp`).

## What it computes

Per department: current headcount, the distinct list of current job titles, the
average current annual base salary, and the most recent hire date — all as of the
**current** SCD-2 row (`is_current = 1`).

## Legacy constructs it exercises (what the reverse stage should identify)

- `GROUP_CONCAT(... SEPARATOR ', ')` — MySQL string aggregation (→ `array_join(collect_list(...))` on Databricks).
- `IFNULL(...)` — → `COALESCE`.
- `DATE_FORMAT(d, '%Y-%m-%d')` and `DATE_SUB(CURDATE(), INTERVAL 90 DAY)` — `%`-style tokens + MySQL date math (must be re-expressed).
- Backtick-quoted identifiers.
- `TINYINT(1)` used as a boolean (`is_current = 1`).
- **Cryptic columns** `hr_comp.salary_history.amt` (annual base salary) and `rsn`
  (change reason) — meaning recovered from data, carried as a spec requirement, not renamed.
- **SCD-2 as-of** filtering that the target must preserve.
- **No cross-schema FK** — `hr_core` ↔ `hr_comp` joined on the shared `employee_id`.

Pair with a `dmig` migration of `hr-mysql` → Databricks; convert this report to a
Databricks SQL script.
