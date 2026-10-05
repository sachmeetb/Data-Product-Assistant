# HR — domain guidance

Free-form guidance for the **HR** domain. Read this before authoring a consumer-aligned data
product over the `samples/hr/` source schema. Covers what's in scope, the two-system layout, the
SCD-2 history, the cross-product join, typical consumer shapes, and naming/PII conventions.

The full scenario design (assumptions register, serving-mode reasoning, the cross-product-join
analysis) lives in `research/hr-demo-scenario.md`.

## What's in scope

HR is modelled as **two systems of record**, loaded as two physically separate Postgres schemas:

- **Core HRIS (`hr_core`)** → source product **`workforce_core`**: the employee master, the org
  (`department`, self-referencing hierarchy), the `job` catalog (grade + salary band), `location`,
  and the SCD-2 histories for **job assignments** and **employment status**, plus periodic
  `performance_review`.
- **Compensation / Payroll (`hr_comp`)** → source product **`compensation_payroll`**: payroll's own
  employee stub (`pay_employee`), the SCD-2 **salary history**, and **benefit enrollment** (+ the
  `benefit_plan` catalog).

Out of scope (not in this dataset, by design): recruiting/ATS, learning/L&D, time & attendance,
absence/leave accrual, payroll runs/payslips, expenses, succession plans.

## The two systems share a key but no FK

`hr_core.employee.employee_id` and `hr_comp.pay_employee.employee_id` are the **same business key**,
but there is **no cross-schema foreign key** — exactly as a real HRIS and payroll system relate.
Each source product is discovered/validated independently. Any consumer product that needs both
(salary + role/department) must **reconcile the join itself on `employee_id`**, and the Workbench
will **not auto-discover it** (FK propagation is single-product-scoped). Author it explicitly:

- a **lookup** transform (e.g. pull `current_salary` from `compensation_payroll` into a
  workforce-primary product), or
- an **explicit join** (`:DatasetTransform.joinsJson`) on `employee_id` for a temporal interval
  join or an aggregate.

Expect FK auto-discovery to **fail first** (`ViewGenerationError: no FK path`) and **succeed once**
the join/lookup is authored — that contrast is intentional (see research doc §4).

## Schema notes

### SCD-2 history tables
`salary_history`, `job_assignment_history`, `employment_status_history`, and `benefit_enrollment`
are all effective-dated with `effective_from` / `effective_to` / `is_current` (benefits use
`enrolled_from` / `enrolled_to`). Exactly one current row per natural key. To read "current",
filter `is_current = true`; to read "as-of date D", filter `effective_from <= D AND (effective_to
IS NULL OR effective_to > D)`.

### Cryptic source columns (resolve via profiling, not the name)
Three columns are deliberately vague and uncommented; their meaning comes from the data:

| Column | Where | Signal | Meaning → standardized name |
|---|---|---|---|
| `amt` | `salary_history` | numeric ~50k–156k | annualised base salary → `salary_amount` |
| `rsn` | `salary_history` | top values MERIT/PROMO/MKT/ADJ | pay-change reason → `change_reason` |
| `lvl` | `job_assignment_history` | smallint 1–8 | job level / grade band → `job_level` |

Metadata enrichment should describe these from the profiled sample; column-name standardization
should recommend the clean names.

### Salary bands
`job.min_salary` / `job.max_salary` define the approved band per job grade. A useful PO rule is
that an employee's `current_salary` should fall within their job's band — a cross-table check.

## Typical consumer-aligned shapes

| Product | Consumes | Serving | History shape |
|---|---|---|---|
| `current_workforce_roster` | both | virtual (`latest_only`) | none — current snapshot; cross-product **lookup** for current salary |
| `compensation_role_history` | both | dbt-materialized (`scd2`) | **consumer-owned effective-dated SCD-2** — temporal interval join of salary × assignment |
| `monthly_headcount_cost` | both | dbt-materialized | **consumer-owned periodic snapshot** — department × month, as-of aggregates |
| `org_directory_safe` | `workforce_core` only | virtual | none — PII-safe directory, single-product baseline (no cross-join) |

The two history products are the point of the domain: a consumer builds its **own** history (a
different shape than any source table), and SCD-2 forces dbt-materialized serving. Starter ODCS
contracts: `playbook/odcs_templates/hr_compensation_role_history.yaml` and
`hr_monthly_headcount_cost.yaml` (current-state summary: `hr_analytics.yaml`).

## Naming & convention notes

- Standardize cryptic source names: `amt → salary_amount`, `rsn → change_reason`, `lvl → job_level`.
- Prefer `*_amount` for money, `*_band` for ordinal buckets (`compensation_band`, `tenure_band`),
  `*_date` for dates, `is_*` for booleans.
- Effective dating uses the `common.yaml` canonicals (`effective_from` / `effective_to` /
  `is_current`); time buckets use `month_of` (don't re-invent them per product).
- Derived metrics (`compensation_band`, `tenure_band`, `pct_change_from_prior`, `headcount`,
  `total_compensation`, `avg_tenure_years`) are in `hr.yaml` under `source_category: derived`.

## PII handling

`employee` carries PII: `date_of_birth`, `national_id_last4`, `email`, names, address-style fields.
For analytics/shareable products: **mask** email (`email_masked`), **mask/suppress**
`national_id_last4`, **suppress** `date_of_birth` (or bucket to an age band), and drop compensation
entirely from directory-style products. The `org_directory_safe` shape is the canonical PII-safe
example; `current_workforce_roster` masks but retains comp for authorized audiences.
