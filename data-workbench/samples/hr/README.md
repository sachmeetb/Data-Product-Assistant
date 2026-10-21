# HR — sample PostgreSQL dataset (two systems of record)

A self-contained HR demo for the Data Workbench, designed to exercise the full
**source-aligned → multiple consumer-aligned** flow, **slowly-changing-dimension (SCD-2)
history**, **consumer products that build their *own* history**, **both serving modes**
(virtual views + dbt-materialized), and a **cross-source-product join**.

Design rationale, the assumptions register, and the cross-product-join analysis live in
**`research/hr-demo-scenario.md`** — read that first for the *why*. This README is the *how*:
load it, then walk the demo.

The data models **two physically separate systems** (two Postgres schemas in one instance):

| Schema | System | Becomes source product |
|---|---|---|
| `hr_core` | Core HRIS (system of record) | **`workforce_core`** |
| `hr_comp` | Compensation / Payroll system | **`compensation_payroll`** |

There is **no cross-schema foreign key**. The only thing linking them is the shared business
key `employee_id` — which the consumer layer must reconcile itself (see "Step 3" + the research
doc §4).

## Contents

| File | Purpose |
|---|---|
| `01_schema.sql` | `CREATE SCHEMA hr_core` + `hr_comp` · 11 tables · FKs · indexes · comments |
| `02_seed.sql` | Deterministic seed (~160 employees, ~2.2k rows total) + integrity DO-block |
| `03_consumer_schema.sql` | `CREATE SCHEMA hr_views` — target for deployed consumer views |

## Loading the data

### Option A — Existing local Postgres
```bash
createdb hr_demo
psql hr_demo -f samples/hr/01_schema.sql
psql hr_demo -f samples/hr/02_seed.sql
psql hr_demo -f samples/hr/03_consumer_schema.sql
```

### Option B — Throwaway Postgres in Docker
```bash
docker rm -f pghr 2>/dev/null
docker run -d --name pghr -p 5433:5432 -e POSTGRES_DB=hr_demo -e POSTGRES_PASSWORD=demo postgres:16
sleep 5
docker exec -i pghr psql -U postgres -d hr_demo < samples/hr/01_schema.sql
docker exec -i pghr psql -U postgres -d hr_demo < samples/hr/02_seed.sql
docker exec -i pghr psql -U postgres -d hr_demo < samples/hr/03_consumer_schema.sql
```

**Two Workbench projects** point at the same DB but different schemas:
- `workforce_core` project → schema `hr_core`
- `compensation_payroll` project → schema `hr_comp`

(From inside the Workbench container, a host DB is reachable as `host.docker.internal`, not `localhost`.)

### Verifying the load
```sql
\dn                         -- hr_core, hr_comp, hr_views
SELECT 'employee' t, count(*) FROM hr_core.employee
UNION ALL SELECT 'department', count(*) FROM hr_core.department
UNION ALL SELECT 'job', count(*) FROM hr_core.job
UNION ALL SELECT 'location', count(*) FROM hr_core.location
UNION ALL SELECT 'job_assignment_history', count(*) FROM hr_core.job_assignment_history
UNION ALL SELECT 'employment_status_history', count(*) FROM hr_core.employment_status_history
UNION ALL SELECT 'performance_review', count(*) FROM hr_core.performance_review
UNION ALL SELECT 'pay_employee', count(*) FROM hr_comp.pay_employee
UNION ALL SELECT 'benefit_plan', count(*) FROM hr_comp.benefit_plan
UNION ALL SELECT 'salary_history', count(*) FROM hr_comp.salary_history
UNION ALL SELECT 'benefit_enrollment', count(*) FROM hr_comp.benefit_enrollment;
```

Expected (deterministic) row counts:

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

The seed prints `NOTICE: HR seed integrity checks passed.` — if any SCD natural key lacks exactly
one current row, or the shared key is misaligned, it raises instead.

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
Three source columns are deliberately **vaguely named and intentionally uncommented** — their
meaning has to come from **profiling the data**, not the name:

| Column | Where | Data signal that disambiguates | Expected enriched meaning → standardized name |
|---|---|---|---|
| `amt` | `hr_comp.salary_history` | numeric, ~50k–156k, 2 dp | annualized base salary → `salary_amount` |
| `rsn` | `hr_comp.salary_history` | top values {MERIT, PROMO, MKT, ADJ} | reason for the pay change → `change_reason` |
| `lvl` | `hr_core.job_assignment_history` | bounded smallint 1–8 | job level / grade band → `job_level` |

What to watch for in the demo: during **Metadata Enrichment** the description should be inferred
from the profiled sample despite the name, and **Column Name Standardization** (dpe-sa) should
recommend the clean name. (Tracked as assumptions #1–#2 in `research/hr-demo-scenario.md` §9.)

---

## Demonstration walkthrough

> Two-person gates (PO source validation, reviews) happen in the web UI; engineering steps can be
> driven from the UI or from Claude Code over MCP. The flow below is archetype `dpe-sa` for the two
> source products, then `dpe-cf` for the four consumer products.

### Step 0 — Load
Load all three SQL files (above). Confirm row counts + the integrity NOTICE.

### Step 1 — Source product 1: `workforce_core` (schema `hr_core`)
1. Create a `dpe-sa` project; set the data source to the `hr_demo` DB, schema `hr_core`.
2. **Data Discovery** — select all 7 `hr_core` tables.
3. **Metadata Enrichment** — confirm descriptions are generated; **watch the cryptic `lvl`**
   column on `job_assignment_history` get described from its 1–8 distribution.
4. **Column Name Standardization** — confirm it recommends `job_level` for `lvl`.
5. **PO Source Validation** — approve names/descriptions/observation rules; **add the PO rules in
   the table below that apply to source product 1**.
6. **Mark Discovery Complete → materialize** `workforce_core`.

### Step 2 — Source product 2: `compensation_payroll` (schema `hr_comp`)
Same flow on schema `hr_comp`. Here the cryptic columns are **`amt` → `salary_amount`** and
**`rsn` → `change_reason`** on `salary_history`; confirm enrichment resolves both from the data.
Materialize `compensation_payroll`.

### Step 3 — Consumer products (dpe-cf): build the four below
Each `CONSUMES` one or both source products. For products that consume **both**, the
`employee_id` join **crosses the product boundary** and is **not auto-discovered** (FK propagation
is single-product-scoped — research doc §4). Author it explicitly:
- **lookup transform** (product A: pull `current_salary` from `compensation_payroll`), or
- **explicit join** (`:DatasetTransform.joinsJson`) on `employee_id` for the temporal interval join
  (product B) and the aggregate (product C).

Expect product B/C to first **fail** FK auto-discovery (`ViewGenerationError: … no FK path`) and
**succeed once** the join/lookup is authored — that contrast is part of the demo.

### Step 4 — Deploy / build
- Products A and D are **virtual** → deploy the `CREATE VIEW` DDL into `hr_views`.
- Products B and C are **dbt-materialized** (B is SCD-2 ⇒ materialized *required*) → build via the
  serving materialization gate.

If the serving stage only *generates* DDL without executing it (as in the products_sales sample),
deploy manually:
```bash
cat projects/<project_code>/serving/virtual_view.sql
(echo "SET search_path TO hr_views, hr_core, hr_comp;"; cat projects/<project_code>/serving/virtual_view.sql) | psql hr_demo
psql hr_demo -c '\dv hr_views.*'
```

---

## Rough PO descriptions (wizard starting points)

Deliberately rough, top-of-mind prose — the kind a Data Product Owner would drop into the wizard's
**Describe** step (`product_idea`) as a starting point, for the Workbench to refine into a tight
description / purpose / schema. They name the *pain* and gesture at the columns without nailing
them down (e.g. "a band is fine", "roughly how long") — that ambiguity is what refinement, the
`hr.yaml` catalog, and the ODCS starter templates resolve.

### A. `current_workforce_roster`
> Our HR business partners and people managers keep rebuilding the same "who works here right now"
> list by hand — they pull the employee table, then try to figure out everyone's *current* job and
> department out of the assignment history, then jump over to the payroll system to grab today's
> salary. It's a different spreadsheet every time and they never quite agree. I just want one
> current-state roster: one row per active person — their name, what they do now, which department
> they're in now, where they're based, roughly how long they've been here, and their current pay (a
> band is fine). Obviously mask the email and don't expose the national ID. Active people only —
> folks who've left or are on leave shouldn't clutter it up.

### B. `compensation_role_history`
> Comp reviews and the occasional pay-equity or audit question always come back to "what was this
> person earning, in what role, on such-and-such date" — and answering it today means hand-stitching
> the salary history together with the job/department history and lining up all the dates. Painful
> and error-prone. I want a proper history: one timeline per employee where each row is a stretch of
> time when both their pay and their role held steady — the salary, the band, the job, the
> department, why the pay changed, and how big the change was. It absolutely has to keep the full
> history, not just the latest, so we can look back at any point in time.

### C. `monthly_headcount_cost`
> Finance and I do this monthly dance where we try to reconstruct how many people were in each
> department and what they cost us, month over month — and everyone's number comes out slightly
> different depending on how they counted. I'd love a clean monthly snapshot: for each department,
> each month, how many active people, the total salary cost, average tenure, and ideally how many we
> hired and how many left that month. Something I can drop straight into the headcount-planning deck
> and trust, that builds up over time so we can actually see the trend.

### D. `org_directory_safe`
> We want a simple company directory anyone across the org can actually use — name, department, job
> title, where someone's based — but every time we try to share one it gets blocked because there's
> PII or salary in it. So just the harmless stuff: no date of birth, no national ID, no pay at all,
> email masked or dropped entirely. Basically a "find a colleague" list that Legal won't object to
> us sharing widely.

## Required columns per consumer-aligned data product

Author these as the consumer product's schema. "Source" indicates which system the value comes
from; transforms reference the Workbench's column-/dataset-level features.

### A. `current_workforce_roster` — virtual · `latest_only` · CONSUMES **both**
One current-state row per active employee.

| Column | Source | Transform |
|---|---|---|
| `employee_id` | core.employee | direct (key) |
| `full_name` | core.employee | concat(first_name, last_name) |
| `email_masked` | core.employee | mask(email, keep_last 4 + keep_format) |
| `department_name` | core.department | lookup-equi via current assignment |
| `job_title` | core.job | lookup-equi via current assignment |
| `work_location` | core.location | lookup-equi |
| `tenure_band` | core.employee.hire_date | bucket (<1y / 1-3 / 3-7 / 7+) |
| `current_salary` | **comp.salary_history** | **cross-product lookup `latest`** (is_current) |
| `employment_status` | core.employee | direct |

*Dataset shape:* `latest_only` (collapse source SCD-2 to current). *Filter:* exclude terminated.

### B. `compensation_role_history` — dbt-materialized · `scd2` · CONSUMES **both** · *consumer-owned history*
The consumer's own effective-dated timeline, reconstructed by a temporal interval-join of two
systems' histories.

| Column | Source | Transform |
|---|---|---|
| `employee_id` | core / comp | direct (key) |
| `effective_from` / `effective_to` / `is_current` | derived | consumer's own SCD-2 dating (interval intersection) |
| `job_title` | core.job (via job_assignment_history) | lookup-equi |
| `job_level` | core.job_assignment_history (`lvl`) | direct (standardized) |
| `department_name` | core.department | lookup-equi |
| `salary_amount` | comp.salary_history (`amt`) | direct (standardized) |
| `compensation_band` | comp.salary_history | bucket (A–E) |
| `change_reason` | comp.salary_history (`rsn`) | direct (standardized) |
| `pct_change_from_prior` | comp.salary_history | window LAG over salary_amount |

*Dataset shape:* `scd2` (effective_from / effective_to / +is_current). *Join:* explicit
`employee_id` interval-join across `salary_history` and `job_assignment_history`.

### C. `monthly_headcount_cost` — dbt-materialized · periodic snapshot · CONSUMES **both** · *consumer-owned history*
One row per department × month — a consumer-accumulated time series.

| Column | Source | Transform |
|---|---|---|
| `department_id` | core | grouping key |
| `department_name` | core.department | lookup-equi |
| `month_of` | derived | grouping key (`date_trunc('month', …)`) |
| `headcount` | core.employment_status_history | aggregate — distinct active as-of month |
| `total_compensation` | comp.salary_history | aggregate — SUM current salary as-of month |
| `avg_tenure_years` | core.employee.hire_date | aggregate — AVG |
| `new_hires` | core | aggregate — COUNT hires in month |
| `terminations` | core.employment_status_history | aggregate — COUNT terminations in month |

*Dataset shape:* grouping_keys (`department_id`, `month_of`) + aggregateFunction; accumulating.

### D. `org_directory_safe` — virtual · `latest_only` · CONSUMES **`workforce_core` only** (baseline, no cross-join)
PII-safe shareable directory; pure intra-product FK joins.

| Column | Source | Transform |
|---|---|---|
| `employee_id` | core.employee | direct |
| `full_name` | core.employee | concat |
| `department_name` | core.department | lookup-equi |
| `job_title` | core.job | lookup-equi |
| `work_location` | core.location | lookup-equi |

*Suppressed (kept in contract/lineage, dropped from the view):* `date_of_birth`,
`national_id_last4`, `email`. No compensation.

---

## A few non-obvious rules for the Data Product Owner to add

Simple to state, but not something discovery/profiling proposes on its own — the PO should add
these in the wizard's Rule Coach / source validation:

1. **(A — roster)** `current_salary` must fall **within the employee's job salary band**
   (`job.min_salary … job.max_salary`). Non-obvious: it's a cross-table comparison, not a fixed range.
2. **(A — roster)** Every row must have `employment_status = 'active'` — the roster is a
   post-filtered invariant (terminated/on-leave must not leak in).
3. **(B — history)** **Exactly one** row per `employee_id` may have `is_current = true` (SCD-2
   integrity). Non-obvious: needs a grouped/uniqueness check, not a per-row rule.
4. **(B — history)** When `change_reason = 'PROMO'`, `pct_change_from_prior` must be **> 0**
   (a promotion that lowered pay is a data error). Cross-field consistency.
5. **(C — monthly)** For any department-month where `headcount = 0`, `total_compensation` must
   also be **0** (no cost without people). Cross-column consistency on aggregates.

(These map to assumptions #4–#10 in `research/hr-demo-scenario.md` §9 — validate they hold, or
catch the seeded data if it doesn't.)

---

## Determinism & re-loading

- All data is generated from id-arithmetic + fixed arrays (no `random()`), so reloads are
  byte-identical. The integrity DO-block fails loudly if generation ever drifts.
- Re-running `01_schema.sql` is destructive (`DROP SCHEMA … CASCADE` on `hr_core`/`hr_comp`).
  `hr_views` is preserved unless dropped manually; redeploy views with `CREATE OR REPLACE VIEW`.
- **Scale knob:** the seed is sized at ~160 employees (4× the original design). To resize, change
  the `generate_series(1, 160)` bounds in `02_seed.sql` for `employee` and `pay_employee` (keep
  them equal — they share the `employee_id` key).

## Companion files (follow-ups, per the design doc)

- `playbook/domain_catalogs/hr.yaml` — exists (current-state); the design proposes adding the
  history/derived columns + rules this scenario needs (research doc §5.1).
- `playbook/transformation_catalogs/hr.yaml` — exists; extend with the cross-product + history
  transforms (§5.2).
- `playbook/odcs_templates/hr_*.yaml` — `hr_analytics.yaml` exists (≈ roster); add the SCD-2 and
  monthly-snapshot consumer templates (§5.3).
- `playbook/domain_catalogs/guidance/hr.md` — to create (§5.4).
