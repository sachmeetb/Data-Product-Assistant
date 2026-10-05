# HR — MySQL sample dataset

A MySQL 8.0+ port of the HR demo for the Data Workbench, exercising the full
**source-aligned → multiple consumer-aligned** flow, **slowly-changing-dimension (SCD-2)
history**, **consumer products that build their own history**, **both serving modes**
(virtual views + dbt-materialized), and a **cross-source-product join**.

This dataset is a faithful translation of `samples/hr/` (Postgres) into MySQL syntax.
Design rationale, the assumptions register, and the cross-product-join analysis live in
**`research/hr-demo-scenario.md`** — read that first for the *why*. This README is the
*how*: load it, then walk the demo.

The data models **two physically separate systems** (two MySQL schemas in one instance):

| Schema | System | Becomes source product |
|---|---|---|
| `hr_core` | Core HRIS (system of record) | **`workforce_core`** |
| `hr_comp` | Compensation / Payroll system | **`compensation_payroll`** |

There is **no cross-schema foreign key**. The only thing linking them is the shared
business key `employee_id` — which the consumer layer must reconcile itself (see
"Step 3" + the research doc §4).

## Contents

| File | Purpose |
|---|---|
| `01_schema.sql` | `CREATE SCHEMA hr_core` + `hr_comp` · 11 tables · FKs · indexes · inline comments |
| `02_seed.sql` | Deterministic seed (~160 employees, ~2.2k rows total) + integrity SELECT checks |
| `03_consumer_schema.sql` | `CREATE SCHEMA hr_views` — target for deployed consumer views |

## MySQL-specific notes

| Postgres feature | MySQL equivalent used here |
|---|---|
| `BOOLEAN` | `TINYINT(1)` (0 = false, 1 = true) |
| `BIGSERIAL` | `BIGINT NOT NULL AUTO_INCREMENT` |
| `TIMESTAMPTZ` | `DATETIME(6)` |
| `NUMERIC(p,s)` | `DECIMAL(p,s)` |
| `generate_series(a,b)` | `WITH RECURSIVE cte(n) AS (SELECT a UNION ALL SELECT n+1 …)` |
| `ARRAY[…][i]` | `ELT(i, …)` |
| `expr \|\| expr` | `CONCAT(expr, expr)` |
| `date + integer` | `DATE_ADD(date, INTERVAL integer DAY)` |
| `UPDATE … FROM` | `UPDATE … JOIN … SET` |
| `COMMENT ON TABLE/COLUMN` | Inline `COMMENT` clause on the column / table |
| Partial index `WHERE cond` | Full composite index (MySQL 8.0 has no partial index support) |
| `DO $$ … $$ LANGUAGE plpgsql` | `SELECT IF(…, 'PASS', 'FAIL')` checks |

**No deferred FK checks.** MySQL InnoDB checks FK constraints at statement level, not
deferred until end-of-transaction. The seed sets `FOREIGN_KEY_CHECKS=0` at the start
and re-enables it before the integrity checks; this is standard MySQL practice for
self-referencing and multi-table bulk loads.

**DATETIME vs TIMESTAMPTZ.** MySQL `DATETIME(6)` stores local time with microsecond
precision. There is no timezone-aware type equivalent to Postgres `TIMESTAMPTZ`. The
demo data uses UTC-equivalent values for `created_at` / `updated_at` columns; no
conversion is needed for the demo scenario.

## Loading the data

### Option A — Existing MySQL 8.0+ instance

```bash
mysql -u root -p < samples/hr-mysql/01_schema.sql
mysql -u root -p < samples/hr-mysql/02_seed.sql
mysql -u root -p < samples/hr-mysql/03_consumer_schema.sql
```

### Option B — Docker MySQL

```bash
docker rm -f mysqlhr 2>/dev/null
docker run -d --name mysqlhr -p 3306:3306 \
  -e MYSQL_ROOT_PASSWORD=demo \
  -e MYSQL_DATABASE=hr_mysql_demo \
  mysql:8.0

sleep 15  # MySQL takes ~15 s to initialize on first run

docker exec -i mysqlhr mysql -uroot -pdemo < samples/hr-mysql/01_schema.sql
docker exec -i mysqlhr mysql -uroot -pdemo < samples/hr-mysql/02_seed.sql
docker exec -i mysqlhr mysql -uroot -pdemo < samples/hr-mysql/03_consumer_schema.sql
```

(From inside the Workbench container, a host DB is reachable as `host.docker.internal`,
not `localhost`.)

### Verifying the load

```sql
SHOW DATABASES LIKE 'hr_%';          -- hr_core, hr_comp, hr_views

SELECT 'employee'                   AS t, COUNT(*) FROM hr_core.employee
UNION ALL SELECT 'department',               COUNT(*) FROM hr_core.department
UNION ALL SELECT 'job',                      COUNT(*) FROM hr_core.job
UNION ALL SELECT 'location',                 COUNT(*) FROM hr_core.location
UNION ALL SELECT 'job_assignment_history',   COUNT(*) FROM hr_core.job_assignment_history
UNION ALL SELECT 'employment_status_history',COUNT(*) FROM hr_core.employment_status_history
UNION ALL SELECT 'performance_review',       COUNT(*) FROM hr_core.performance_review
UNION ALL SELECT 'pay_employee',             COUNT(*) FROM hr_comp.pay_employee
UNION ALL SELECT 'benefit_plan',             COUNT(*) FROM hr_comp.benefit_plan
UNION ALL SELECT 'salary_history',           COUNT(*) FROM hr_comp.salary_history
UNION ALL SELECT 'benefit_enrollment',       COUNT(*) FROM hr_comp.benefit_enrollment;
```

Expected (deterministic) row counts — identical to the Postgres original:

| Schema.table | Rows |
|---|---:|
| `hr_core.department` | 12 |
| `hr_core.job` | 16 |
| `hr_core.location` | 8 |
| `hr_core.employee` | 160 |
| `hr_core.job_assignment_history` | 320 |
| `hr_core.employment_status_history` | 181 |
| `hr_core.performance_review` | 444 |
| `hr_comp.pay_employee` | 160 |
| `hr_comp.benefit_plan` | 10 |
| `hr_comp.salary_history` | 480 |
| `hr_comp.benefit_enrollment` | 342 |

The seed prints PASS for each of the five integrity SELECT checks at the end of
`02_seed.sql`. If any check prints FAIL, the deterministic generation drifted.

## Registering the connection in the Workbench

The Workbench connects to MySQL via a named `PlatformConnection` rather than a raw DSN.
This replaces the single Postgres connection-string step from the sibling demo.

**Step 1 — Register the connection** (once per host):

```
POST /api/connections
{
  "connection_name": "hr-mysql-demo",
  "platform_type":  "mysql",
  "host":           "localhost",
  "port":           3306,
  "database":       "hr_mysql_demo",
  "username":       "root",
  "secret_ref":     "MYSQL_ROOT_PASSWORD"
}
```

Set `MYSQL_ROOT_PASSWORD=demo` in the backend `.env` before starting the server.
From inside the Workbench container, use `host.docker.internal` instead of `localhost`.

**Step 2 — Bind each project to the connection**:

```
PUT /api/projects/{id}/source-binding
{
  "connection_id":    <id returned in step 1>,
  "default_schema":   "hr_core"    ← use "hr_comp" for the compensation project
}
```

**Two Workbench projects** point at the same MySQL instance but different schemas:
- `workforce_core` project → `default_schema: hr_core`
- `compensation_payroll` project → `default_schema: hr_comp`

## Schema overview

```
   hr_core (Core HRIS)                          hr_comp (Comp / Payroll)
   ───────────────────                          ────────────────────────
   department (self-FK hierarchy)               pay_employee (shared employee_id)
   job (grade, salary band)                       │
   location                                       ├── salary_history       (SCD-2)
   employee (PII) ──┬── job_assignment_history    └── benefit_enrollment   (SCD-2) ── benefit_plan
                    │     (SCD-2: job/dept/loc)
                    ├── employment_status_history (SCD-2: active/on_leave/terminated)
                    └── performance_review        (periodic fact)

          employee.employee_id  ⟵ shared business key, NO FK ⟶  pay_employee.employee_id
```

### Cryptic columns (intentional stress test for metadata enrichment)

Three source columns are deliberately **vaguely named and intentionally uncommented** —
their meaning has to come from **profiling the data**, not the name:

| Column | Where | Data signal that disambiguates | Expected enriched meaning → standardized name |
|---|---|---|---|
| `amt` | `hr_comp.salary_history` | numeric, ~50k–156k, 2 dp | annualized base salary → `salary_amount` |
| `rsn` | `hr_comp.salary_history` | top values {MERIT, PROMO, MKT, ADJ} | reason for the pay change → `change_reason` |
| `lvl` | `hr_core.job_assignment_history` | bounded smallint 1–8 | job level / grade band → `job_level` |

---

## Demonstration walkthrough

> Two-person gates (PO source validation, reviews) happen in the web UI; engineering
> steps can be driven from the UI or from Claude Code over MCP. The flow below is
> archetype `dpe-sa` for the two source products, then `dpe-cf` for four consumer
> products.

### Step 0 — Load
Load all three SQL files (above). Confirm row counts and that each integrity check
returns PASS.

### Step 1 — Source product 1: `workforce_core` (schema `hr_core`)
1. Create a `dpe-sa` project; bind the data source to the `hr-mysql-demo` connection,
   schema `hr_core`.
2. **Data Discovery** — select all 7 `hr_core` tables.
3. **Metadata Enrichment** — confirm descriptions are generated; **watch the cryptic
   `lvl`** column on `job_assignment_history` get described from its 1–8 distribution.
4. **Column Name Standardization** — confirm it recommends `job_level` for `lvl`.
5. **PO Source Validation** — approve names/descriptions/observation rules; add the PO
   rules listed below that apply to source product 1.
6. **Mark Discovery Complete → materialize** `workforce_core`.

### Step 2 — Source product 2: `compensation_payroll` (schema `hr_comp`)
Same flow on schema `hr_comp`. Here the cryptic columns are **`amt` → `salary_amount`**
and **`rsn` → `change_reason`** on `salary_history`; confirm enrichment resolves both
from the data. Materialize `compensation_payroll`.

### Step 3 — Consumer products (dpe-cf): build the four below
Each `CONSUMES` one or both source products. For products that consume **both**, the
`employee_id` join **crosses the product boundary** and is **not auto-discovered**
(FK propagation is single-product-scoped — research doc §4). Author it explicitly:
- **lookup transform** (product A: pull `current_salary` from `compensation_payroll`), or
- **explicit join** (`:DatasetTransform.joinsJson`) on `employee_id` for the temporal
  interval join (product B) and the aggregate (product C).

Expect products B/C to first **fail** FK auto-discovery (`ViewGenerationError: … no FK
path`) and **succeed once** the join/lookup is authored — that contrast is part of the
demo.

### Step 4 — Deploy / build
- Products A and D are **virtual** → deploy the CREATE VIEW DDL into `hr_views`.
- Products B and C are **dbt-materialized** (B is SCD-2 ⇒ materialized *required*) →
  build via the serving materialization gate.

MySQL note: the Workbench generates fully-qualified view DDL (`hr_views.vw_<dataset>`)
so no `USE` or `SET search_path` step is needed before deployment.

---

## Rough PO descriptions (wizard starting points)

Deliberately rough, top-of-mind prose — see the Postgres `samples/hr/README.md` for
the full descriptions and the Required columns per product. The text is identical;
only the connection setup differs.

---

## A few non-obvious rules for the Data Product Owner to add

1. **(A — roster)** `current_salary` must fall **within the employee's job salary band**
   (`job.min_salary … job.max_salary`).
2. **(A — roster)** Every row must have `employment_status = 'active'`.
3. **(B — history)** **Exactly one** row per `employee_id` may have `is_current = 1`
   (SCD-2 integrity). Note: MySQL uses `1` instead of `true` for TINYINT(1).
4. **(B — history)** When `change_reason = 'PROMO'`, `pct_change_from_prior` must be
   **> 0**.
5. **(C — monthly)** For any department-month where `headcount = 0`,
   `total_compensation` must also be **0**.

---

## Determinism & re-loading

- All data is generated from id-arithmetic + fixed arrays (no `RAND()`), so reloads
  are byte-identical. The integrity checks at the end of `02_seed.sql` fail loudly if
  the generation ever drifts.
- Re-running `01_schema.sql` is destructive (`DROP SCHEMA … ` on `hr_core`/`hr_comp`).
  `hr_views` is preserved unless dropped manually; redeploy views with
  `CREATE OR REPLACE VIEW`.
- **Scale knob:** change the upper bound in the `WHERE e < 160` clause of the employee
  and pay_employee recursive CTEs in `02_seed.sql` (keep them equal — they share the
  `employee_id` key).

## Companion files

- `playbook/domain_catalogs/hr.yaml` — domain catalog (current-state).
- `playbook/transformation_catalogs/hr.yaml` — transformation hints.
- `playbook/odcs_templates/hr_analytics.yaml` — ODCS starter template (≈ roster).
- `research/hr-demo-scenario.md` — full design rationale, assumptions register,
  cross-product-join analysis.
