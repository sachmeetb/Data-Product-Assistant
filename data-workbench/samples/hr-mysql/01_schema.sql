-- ============================================================================
-- HR demo dataset — schema, MySQL 8.0+ (two physically separate systems)
-- ============================================================================
-- Companion design: research/hr-demo-scenario.md
-- MySQL port of samples/hr/01_schema.sql.  Do NOT modify the Postgres originals.
--
-- Two MySQL schemas (= databases) in ONE instance, modelling two real HR systems:
--   hr_core  — Core HRIS (system of record)        → source product "workforce_core"
--   hr_comp  — Compensation / Payroll system        → source product "compensation_payroll"
--
-- They are PHYSICALLY isolated (separate schemas), not just logically scoped.
-- There is NO cross-schema foreign key.  The only link is the shared business
-- key `employee_id` (hr_core.employee.employee_id == hr_comp.pay_employee.employee_id),
-- which the consumer layer must reconcile itself (see research doc §4).
--
-- MySQL notes vs the Postgres original:
--   • BOOLEAN → TINYINT(1) (0 = false, 1 = true)
--   • BIGSERIAL → BIGINT NOT NULL AUTO_INCREMENT
--   • TIMESTAMPTZ → DATETIME(6)
--   • NUMERIC(p,s) → DECIMAL(p,s)
--   • COMMENT ON TABLE/COLUMN → inline COMMENT clauses
--   • Partial indexes not supported; WHERE is_current replaced by composite index
--   • Every table uses ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
--
-- A few columns are DELIBERATELY cryptic (amt, rsn, lvl) and intentionally carry
-- NO column comment — their meaning must be recovered from PROFILED DATA during
-- metadata enrichment, not from the name.  (See README "Cryptic columns".)
--
-- Re-running is destructive: both schemas are dropped and recreated.  The
-- consumer-view schema (hr_views) is created separately by 03_consumer_schema.sql.
-- ============================================================================

DROP SCHEMA IF EXISTS hr_core;
DROP SCHEMA IF EXISTS hr_comp;

CREATE SCHEMA IF NOT EXISTS hr_core;
CREATE SCHEMA IF NOT EXISTS hr_comp;

-- ===========================================================================
-- hr_core — Core HRIS
-- ===========================================================================

CREATE TABLE IF NOT EXISTS hr_core.department (
    department_id        INT          NOT NULL,
    dept_name            VARCHAR(80)  NOT NULL,
    cost_center          VARCHAR(12)  NOT NULL   COMMENT 'Finance cost center the department rolls up to.',
    parent_department_id INT                     COMMENT 'Parent department; NULL for top-level org units.',
    created_at           DATETIME(6)  NOT NULL   DEFAULT '2015-01-01 00:00:00',
    updated_at           DATETIME(6)  NOT NULL   DEFAULT '2024-12-31 00:00:00',
    PRIMARY KEY (department_id),
    CONSTRAINT fk_dept_parent FOREIGN KEY (parent_department_id)
        REFERENCES hr_core.department(department_id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Organizational units; self-referencing hierarchy via parent_department_id.';

CREATE TABLE IF NOT EXISTS hr_core.job (
    job_id      INT           NOT NULL,
    job_code    VARCHAR(12)   NOT NULL,
    job_title   VARCHAR(80)   NOT NULL,
    job_family  VARCHAR(40)   NOT NULL,
    grade       SMALLINT      NOT NULL   COMMENT 'Job grade (1=junior … 8=executive).',
    min_salary  DECIMAL(12,2) NOT NULL   COMMENT 'Bottom of the approved annual salary band for the job.',
    max_salary  DECIMAL(12,2) NOT NULL   COMMENT 'Top of the approved annual salary band for the job.',
    PRIMARY KEY (job_id),
    UNIQUE KEY uk_job_code (job_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Job catalog: title, family, grade, and the approved salary band.';

CREATE TABLE IF NOT EXISTS hr_core.location (
    location_id  INT         NOT NULL,
    site_name    VARCHAR(60) NOT NULL,
    city         VARCHAR(60) NOT NULL,
    country_code CHAR(2)     NOT NULL   COMMENT 'ISO 3166-1 alpha-2 country code.',
    region       VARCHAR(8)  NOT NULL,
    PRIMARY KEY (location_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Physical work sites / offices.';

CREATE TABLE IF NOT EXISTS hr_core.employee (
    employee_id        INT          NOT NULL,
    first_name         VARCHAR(40)  NOT NULL,
    last_name          VARCHAR(40)  NOT NULL,
    email              VARCHAR(120) NOT NULL,
    date_of_birth      DATE         NOT NULL   COMMENT 'Employee date of birth (PII).',
    gender             CHAR(1)      NOT NULL   COMMENT 'Self-reported gender: M, F, or X (non-binary/undisclosed).',
    national_id_last4  CHAR(4)      NOT NULL   COMMENT 'Last four digits of national ID (PII).',
    hire_date          DATE         NOT NULL,
    department_id      INT          NOT NULL   COMMENT 'Current home department (synced from the current job assignment).',
    manager_id         INT                     COMMENT 'Current line manager (self-reference); NULL for org leaders.',
    employment_status  VARCHAR(16)  NOT NULL   DEFAULT 'active'
                                               COMMENT 'Current lifecycle status: active, on_leave, terminated.',
    created_at         DATETIME(6)  NOT NULL   DEFAULT '2015-01-01 00:00:00',
    updated_at         DATETIME(6)  NOT NULL   DEFAULT '2024-12-31 00:00:00',
    PRIMARY KEY (employee_id),
    UNIQUE KEY uk_emp_email (email),
    CONSTRAINT fk_emp_dept FOREIGN KEY (department_id)
        REFERENCES hr_core.department(department_id),
    CONSTRAINT fk_emp_mgr  FOREIGN KEY (manager_id)
        REFERENCES hr_core.employee(employee_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Employee master (current-state) in the core HRIS. Contains PII.';

CREATE TABLE IF NOT EXISTS hr_core.job_assignment_history (
    assignment_id   BIGINT      NOT NULL AUTO_INCREMENT,
    employee_id     INT         NOT NULL,
    job_id          INT         NOT NULL,
    department_id   INT         NOT NULL,
    location_id     INT         NOT NULL,
    lvl             SMALLINT    NOT NULL,   -- cryptic: NO comment by design
    assignment_type VARCHAR(16) NOT NULL   COMMENT 'What created the assignment: hire, promotion, transfer, reorg.',
    effective_from  DATE        NOT NULL   COMMENT 'Inclusive start of the period this assignment was active.',
    effective_to    DATE                   COMMENT 'Exclusive end of the period; NULL means currently active.',
    is_current      TINYINT(1)  NOT NULL   COMMENT 'True (1) for the row currently in effect.',
    PRIMARY KEY (assignment_id),
    UNIQUE KEY uk_jah_emp_efrom (employee_id, effective_from),
    CONSTRAINT fk_jah_emp  FOREIGN KEY (employee_id)  REFERENCES hr_core.employee(employee_id),
    CONSTRAINT fk_jah_job  FOREIGN KEY (job_id)       REFERENCES hr_core.job(job_id),
    CONSTRAINT fk_jah_dept FOREIGN KEY (department_id) REFERENCES hr_core.department(department_id),
    CONSTRAINT fk_jah_loc  FOREIGN KEY (location_id)  REFERENCES hr_core.location(location_id),
    CHECK (effective_to IS NULL OR effective_to > effective_from)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='SCD-2 history of an employee''s job/department/location assignments over time.';
-- NOTE: column `lvl` is intentionally cryptic and intentionally uncommented.

CREATE TABLE IF NOT EXISTS hr_core.employment_status_history (
    status_id         BIGINT      NOT NULL AUTO_INCREMENT,
    employee_id       INT         NOT NULL,
    employment_status VARCHAR(16) NOT NULL,
    status_reason     VARCHAR(40)            COMMENT 'Reason for the status change (e.g. voluntary, medical).',
    effective_from    DATE        NOT NULL,
    effective_to      DATE,
    is_current        TINYINT(1)  NOT NULL,
    PRIMARY KEY (status_id),
    UNIQUE KEY uk_esh_emp_efrom (employee_id, effective_from),
    CONSTRAINT fk_esh_emp FOREIGN KEY (employee_id)
        REFERENCES hr_core.employee(employee_id),
    CHECK (effective_to IS NULL OR effective_to > effective_from)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='SCD-2 history of employment lifecycle: active, on_leave, terminated.';

CREATE TABLE IF NOT EXISTS hr_core.performance_review (
    review_id    BIGINT      NOT NULL AUTO_INCREMENT,
    employee_id  INT         NOT NULL,
    review_cycle VARCHAR(12) NOT NULL   COMMENT 'Review period label, e.g. 2024-H1.',
    rating       SMALLINT    NOT NULL   COMMENT 'Performance rating 1 (low) … 5 (high).',
    reviewer_id  INT,
    review_date  DATE        NOT NULL,
    PRIMARY KEY (review_id),
    UNIQUE KEY uk_perf_emp_cycle (employee_id, review_cycle),
    CONSTRAINT fk_perf_emp      FOREIGN KEY (employee_id) REFERENCES hr_core.employee(employee_id),
    CONSTRAINT fk_perf_reviewer FOREIGN KEY (reviewer_id) REFERENCES hr_core.employee(employee_id),
    CHECK (rating BETWEEN 1 AND 5)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Periodic performance reviews — one row per employee per review cycle.';

CREATE INDEX ix_jah_emp  ON hr_core.job_assignment_history(employee_id);
-- ix_jah_cur: MySQL does not support partial (filtered) indexes.
-- Using a composite index to approximate the WHERE is_current = 1 access pattern.
CREATE INDEX ix_jah_cur  ON hr_core.job_assignment_history(employee_id, is_current);
CREATE INDEX ix_esh_emp  ON hr_core.employment_status_history(employee_id);
CREATE INDEX ix_perf_emp ON hr_core.performance_review(employee_id);
CREATE INDEX ix_emp_dept ON hr_core.employee(department_id);

-- ===========================================================================
-- hr_comp — Compensation / Payroll system
-- ===========================================================================

CREATE TABLE IF NOT EXISTS hr_comp.pay_employee (
    employee_id   INT          NOT NULL   COMMENT 'Shared business key matching hr_core.employee.employee_id (reconciled at the consumer layer).',
    pay_group     VARCHAR(16)  NOT NULL   COMMENT 'Payroll grouping / run assignment.',
    payroll_no    VARCHAR(16)  NOT NULL,
    pay_frequency VARCHAR(16)  NOT NULL   COMMENT 'How often the employee is paid: biweekly, semimonthly, monthly.',
    PRIMARY KEY (employee_id),
    UNIQUE KEY uk_payroll_no (payroll_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Payroll''s own employee record. employee_id is the shared business key with the HRIS; there is intentionally NO cross-schema FK.';

CREATE TABLE IF NOT EXISTS hr_comp.benefit_plan (
    plan_code  VARCHAR(8)  NOT NULL,
    plan_name  VARCHAR(60) NOT NULL,
    plan_type  VARCHAR(16) NOT NULL   COMMENT 'Plan category: health, dental, vision, retirement, life.',
    tier       VARCHAR(16) NOT NULL,
    PRIMARY KEY (plan_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Catalog of benefit plans (health, dental, vision, retirement, life).';

CREATE TABLE IF NOT EXISTS hr_comp.salary_history (
    salary_id      BIGINT        NOT NULL AUTO_INCREMENT,
    employee_id    INT           NOT NULL,
    amt            DECIMAL(12,2) NOT NULL,   -- cryptic: NO comment by design
    currency_code  CHAR(3)       NOT NULL   DEFAULT 'USD'
                                             COMMENT 'ISO 4217 currency code of the pay amount.',
    rsn            VARCHAR(8),               -- cryptic: NO comment by design
    prior_amt      DECIMAL(12,2)             COMMENT 'The pay amount immediately before this change (NULL for the first record).',
    effective_from DATE          NOT NULL   COMMENT 'Inclusive start of the period this pay amount was active.',
    effective_to   DATE                     COMMENT 'Exclusive end of the period; NULL means currently active.',
    is_current     TINYINT(1)    NOT NULL   COMMENT 'True (1) for the pay amount currently in effect.',
    PRIMARY KEY (salary_id),
    UNIQUE KEY uk_sal_emp_efrom (employee_id, effective_from),
    CONSTRAINT fk_sal_emp FOREIGN KEY (employee_id)
        REFERENCES hr_comp.pay_employee(employee_id),
    CHECK (effective_to IS NULL OR effective_to > effective_from)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='SCD-2 history of employee base pay over time.';
-- NOTE: columns `amt` and `rsn` are intentionally cryptic and intentionally uncommented.

CREATE TABLE IF NOT EXISTS hr_comp.benefit_enrollment (
    enrollment_id  BIGINT      NOT NULL AUTO_INCREMENT,
    employee_id    INT         NOT NULL,
    plan_code      VARCHAR(8)  NOT NULL,
    coverage_level VARCHAR(16) NOT NULL   COMMENT 'Coverage tier: employee, employee+1, family.',
    enrolled_from  DATE        NOT NULL,
    enrolled_to    DATE,
    is_current     TINYINT(1)  NOT NULL   COMMENT 'True (1) for the enrollment currently in effect for this employee+plan.',
    PRIMARY KEY (enrollment_id),
    UNIQUE KEY uk_ben_emp_plan_efrom (employee_id, plan_code, enrolled_from),
    CONSTRAINT fk_benent_emp  FOREIGN KEY (employee_id)
        REFERENCES hr_comp.pay_employee(employee_id),
    CONSTRAINT fk_benent_plan FOREIGN KEY (plan_code)
        REFERENCES hr_comp.benefit_plan(plan_code),
    CHECK (enrolled_to IS NULL OR enrolled_to > enrolled_from)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='SCD-2 history of benefit-plan enrollments per employee.';

CREATE INDEX ix_sal_emp  ON hr_comp.salary_history(employee_id);
-- ix_sal_cur: MySQL does not support partial (filtered) indexes.
-- Using a composite index to approximate the WHERE is_current = 1 access pattern.
CREATE INDEX ix_sal_cur  ON hr_comp.salary_history(employee_id, is_current);
CREATE INDEX ix_ben_emp  ON hr_comp.benefit_enrollment(employee_id);
CREATE INDEX ix_ben_plan ON hr_comp.benefit_enrollment(plan_code);
