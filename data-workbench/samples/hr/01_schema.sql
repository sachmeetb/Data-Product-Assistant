-- ============================================================================
-- HR demo dataset — schema (two physically separate systems of record)
-- ============================================================================
-- Companion design: research/hr-demo-scenario.md
--
-- Two Postgres schemas in ONE instance, modelling two real HR systems:
--   hr_core  — Core HRIS (system of record)        → source product "workforce_core"
--   hr_comp  — Compensation / Payroll system        → source product "compensation_payroll"
--
-- They are PHYSICALLY isolated (separate schemas), not just logically scoped.
-- There is NO cross-schema foreign key. The only link is the shared business
-- key `employee_id` (hr_core.employee.employee_id == hr_comp.pay_employee.employee_id),
-- which the consumer layer must reconcile itself (see research doc §4).
--
-- A few columns are DELIBERATELY cryptic (amt, rsn, lvl) and intentionally carry
-- NO column comment — their meaning must be recovered from PROFILED DATA during
-- metadata enrichment, not from the name. (See README "Cryptic columns".)
--
-- Re-running is destructive: both schemas are dropped and recreated. The
-- consumer-view schema (hr_views) is created separately by 03_consumer_schema.sql.
-- ============================================================================

DROP SCHEMA IF EXISTS hr_core CASCADE;
DROP SCHEMA IF EXISTS hr_comp CASCADE;

CREATE SCHEMA hr_core;
CREATE SCHEMA hr_comp;

-- ===========================================================================
-- hr_core — Core HRIS
-- ===========================================================================

CREATE TABLE hr_core.department (
    department_id        integer PRIMARY KEY,
    dept_name            varchar(80)  NOT NULL,
    cost_center          varchar(12)  NOT NULL,
    parent_department_id integer      REFERENCES hr_core.department(department_id),
    created_at           timestamptz  NOT NULL DEFAULT '2015-01-01T00:00:00Z',
    updated_at           timestamptz  NOT NULL DEFAULT '2024-12-31T00:00:00Z'
);
COMMENT ON TABLE  hr_core.department IS 'Organizational units; self-referencing hierarchy via parent_department_id.';
COMMENT ON COLUMN hr_core.department.cost_center IS 'Finance cost center the department rolls up to.';
COMMENT ON COLUMN hr_core.department.parent_department_id IS 'Parent department; NULL for top-level org units.';

CREATE TABLE hr_core.job (
    job_id      integer PRIMARY KEY,
    job_code    varchar(12)  NOT NULL UNIQUE,
    job_title   varchar(80)  NOT NULL,
    job_family  varchar(40)  NOT NULL,
    grade       smallint     NOT NULL,
    min_salary  numeric(12,2) NOT NULL,
    max_salary  numeric(12,2) NOT NULL
);
COMMENT ON TABLE  hr_core.job IS 'Job catalog: title, family, grade, and the approved salary band.';
COMMENT ON COLUMN hr_core.job.grade IS 'Job grade (1=junior … 8=executive).';
COMMENT ON COLUMN hr_core.job.min_salary IS 'Bottom of the approved annual salary band for the job.';
COMMENT ON COLUMN hr_core.job.max_salary IS 'Top of the approved annual salary band for the job.';

CREATE TABLE hr_core.location (
    location_id  integer PRIMARY KEY,
    site_name    varchar(60) NOT NULL,
    city         varchar(60) NOT NULL,
    country_code char(2)     NOT NULL,
    region       varchar(8)  NOT NULL
);
COMMENT ON TABLE  hr_core.location IS 'Physical work sites / offices.';
COMMENT ON COLUMN hr_core.location.country_code IS 'ISO 3166-1 alpha-2 country code.';

CREATE TABLE hr_core.employee (
    employee_id        integer PRIMARY KEY,
    first_name         varchar(40) NOT NULL,
    last_name          varchar(40) NOT NULL,
    email              varchar(120) NOT NULL UNIQUE,
    date_of_birth      date        NOT NULL,
    gender             char(1)     NOT NULL,
    national_id_last4  char(4)     NOT NULL,
    hire_date          date        NOT NULL,
    department_id      integer     NOT NULL REFERENCES hr_core.department(department_id),
    manager_id         integer     REFERENCES hr_core.employee(employee_id),
    employment_status  varchar(16) NOT NULL DEFAULT 'active',
    created_at         timestamptz NOT NULL DEFAULT '2015-01-01T00:00:00Z',
    updated_at         timestamptz NOT NULL DEFAULT '2024-12-31T00:00:00Z'
);
COMMENT ON TABLE  hr_core.employee IS 'Employee master (current-state) in the core HRIS. Contains PII.';
COMMENT ON COLUMN hr_core.employee.date_of_birth IS 'Employee date of birth (PII).';
COMMENT ON COLUMN hr_core.employee.gender IS 'Self-reported gender: M, F, or X (non-binary/undisclosed).';
COMMENT ON COLUMN hr_core.employee.national_id_last4 IS 'Last four digits of national ID (PII).';
COMMENT ON COLUMN hr_core.employee.department_id IS 'Current home department (synced from the current job assignment).';
COMMENT ON COLUMN hr_core.employee.manager_id IS 'Current line manager (self-reference); NULL for org leaders.';
COMMENT ON COLUMN hr_core.employee.employment_status IS 'Current lifecycle status: active, on_leave, terminated.';

CREATE TABLE hr_core.job_assignment_history (
    assignment_id   bigserial PRIMARY KEY,
    employee_id     integer NOT NULL REFERENCES hr_core.employee(employee_id),
    job_id          integer NOT NULL REFERENCES hr_core.job(job_id),
    department_id   integer NOT NULL REFERENCES hr_core.department(department_id),
    location_id     integer NOT NULL REFERENCES hr_core.location(location_id),
    lvl             smallint NOT NULL,                                  -- cryptic: NO comment by design
    assignment_type varchar(16) NOT NULL,
    effective_from  date NOT NULL,
    effective_to    date,
    is_current      boolean NOT NULL,
    UNIQUE (employee_id, effective_from),
    CHECK (effective_to IS NULL OR effective_to > effective_from)
);
COMMENT ON TABLE  hr_core.job_assignment_history IS 'SCD-2 history of an employee''s job/department/location assignments over time.';
COMMENT ON COLUMN hr_core.job_assignment_history.assignment_type IS 'What created the assignment: hire, promotion, transfer, reorg.';
COMMENT ON COLUMN hr_core.job_assignment_history.effective_from IS 'Inclusive start of the period this assignment was active.';
COMMENT ON COLUMN hr_core.job_assignment_history.effective_to IS 'Exclusive end of the period; NULL means currently active.';
COMMENT ON COLUMN hr_core.job_assignment_history.is_current IS 'True for the row currently in effect.';
-- NOTE: column `lvl` is intentionally cryptic and intentionally uncommented.

CREATE TABLE hr_core.employment_status_history (
    status_id        bigserial PRIMARY KEY,
    employee_id      integer NOT NULL REFERENCES hr_core.employee(employee_id),
    employment_status varchar(16) NOT NULL,
    status_reason    varchar(40),
    effective_from   date NOT NULL,
    effective_to     date,
    is_current       boolean NOT NULL,
    UNIQUE (employee_id, effective_from),
    CHECK (effective_to IS NULL OR effective_to > effective_from)
);
COMMENT ON TABLE  hr_core.employment_status_history IS 'SCD-2 history of employment lifecycle: active, on_leave, terminated.';
COMMENT ON COLUMN hr_core.employment_status_history.status_reason IS 'Reason for the status change (e.g. voluntary, medical).';

CREATE TABLE hr_core.performance_review (
    review_id    bigserial PRIMARY KEY,
    employee_id  integer NOT NULL REFERENCES hr_core.employee(employee_id),
    review_cycle varchar(12) NOT NULL,
    rating       smallint NOT NULL,
    reviewer_id  integer REFERENCES hr_core.employee(employee_id),
    review_date  date NOT NULL,
    UNIQUE (employee_id, review_cycle),
    CHECK (rating BETWEEN 1 AND 5)
);
COMMENT ON TABLE  hr_core.performance_review IS 'Periodic performance reviews — one row per employee per review cycle.';
COMMENT ON COLUMN hr_core.performance_review.review_cycle IS 'Review period label, e.g. 2024-H1.';
COMMENT ON COLUMN hr_core.performance_review.rating IS 'Performance rating 1 (low) … 5 (high).';

CREATE INDEX ix_jah_emp   ON hr_core.job_assignment_history(employee_id);
CREATE INDEX ix_jah_cur   ON hr_core.job_assignment_history(employee_id) WHERE is_current;
CREATE INDEX ix_esh_emp   ON hr_core.employment_status_history(employee_id);
CREATE INDEX ix_perf_emp  ON hr_core.performance_review(employee_id);
CREATE INDEX ix_emp_dept  ON hr_core.employee(department_id);

-- ===========================================================================
-- hr_comp — Compensation / Payroll system
-- ===========================================================================

CREATE TABLE hr_comp.pay_employee (
    employee_id   integer PRIMARY KEY,            -- shared business key with hr_core.employee (NO FK across schemas)
    pay_group     varchar(16) NOT NULL,
    payroll_no    varchar(16) NOT NULL UNIQUE,
    pay_frequency varchar(16) NOT NULL
);
COMMENT ON TABLE  hr_comp.pay_employee IS 'Payroll''s own employee record. employee_id is the shared business key with the HRIS; there is intentionally NO cross-schema FK.';
COMMENT ON COLUMN hr_comp.pay_employee.employee_id IS 'Shared business key matching hr_core.employee.employee_id (reconciled at the consumer layer).';
COMMENT ON COLUMN hr_comp.pay_employee.pay_group IS 'Payroll grouping / run assignment.';
COMMENT ON COLUMN hr_comp.pay_employee.pay_frequency IS 'How often the employee is paid: biweekly, semimonthly, monthly.';

CREATE TABLE hr_comp.benefit_plan (
    plan_code  varchar(8) PRIMARY KEY,
    plan_name  varchar(60) NOT NULL,
    plan_type  varchar(16) NOT NULL,
    tier       varchar(16) NOT NULL
);
COMMENT ON TABLE  hr_comp.benefit_plan IS 'Catalog of benefit plans (health, dental, vision, retirement, life).';
COMMENT ON COLUMN hr_comp.benefit_plan.plan_type IS 'Plan category: health, dental, vision, retirement, life.';

CREATE TABLE hr_comp.salary_history (
    salary_id      bigserial PRIMARY KEY,
    employee_id    integer NOT NULL REFERENCES hr_comp.pay_employee(employee_id),
    amt            numeric(12,2) NOT NULL,            -- cryptic: NO comment by design
    currency_code  char(3) NOT NULL DEFAULT 'USD',
    rsn            varchar(8),                         -- cryptic: NO comment by design
    prior_amt      numeric(12,2),
    effective_from date NOT NULL,
    effective_to   date,
    is_current     boolean NOT NULL,
    UNIQUE (employee_id, effective_from),
    CHECK (effective_to IS NULL OR effective_to > effective_from)
);
COMMENT ON TABLE  hr_comp.salary_history IS 'SCD-2 history of employee base pay over time.';
COMMENT ON COLUMN hr_comp.salary_history.currency_code IS 'ISO 4217 currency code of the pay amount.';
COMMENT ON COLUMN hr_comp.salary_history.prior_amt IS 'The pay amount immediately before this change (NULL for the first record).';
COMMENT ON COLUMN hr_comp.salary_history.effective_from IS 'Inclusive start of the period this pay amount was active.';
COMMENT ON COLUMN hr_comp.salary_history.effective_to IS 'Exclusive end of the period; NULL means currently active.';
COMMENT ON COLUMN hr_comp.salary_history.is_current IS 'True for the pay amount currently in effect.';
-- NOTE: columns `amt` and `rsn` are intentionally cryptic and intentionally uncommented.

CREATE TABLE hr_comp.benefit_enrollment (
    enrollment_id  bigserial PRIMARY KEY,
    employee_id    integer NOT NULL REFERENCES hr_comp.pay_employee(employee_id),
    plan_code      varchar(8) NOT NULL REFERENCES hr_comp.benefit_plan(plan_code),
    coverage_level varchar(16) NOT NULL,
    enrolled_from  date NOT NULL,
    enrolled_to    date,
    is_current     boolean NOT NULL,
    UNIQUE (employee_id, plan_code, enrolled_from),
    CHECK (enrolled_to IS NULL OR enrolled_to > enrolled_from)
);
COMMENT ON TABLE  hr_comp.benefit_enrollment IS 'SCD-2 history of benefit-plan enrollments per employee.';
COMMENT ON COLUMN hr_comp.benefit_enrollment.coverage_level IS 'Coverage tier: employee, employee+1, family.';
COMMENT ON COLUMN hr_comp.benefit_enrollment.is_current IS 'True for the enrollment currently in effect for this employee+plan.';

CREATE INDEX ix_sal_emp   ON hr_comp.salary_history(employee_id);
CREATE INDEX ix_sal_cur   ON hr_comp.salary_history(employee_id) WHERE is_current;
CREATE INDEX ix_ben_emp   ON hr_comp.benefit_enrollment(employee_id);
CREATE INDEX ix_ben_plan  ON hr_comp.benefit_enrollment(plan_code);
