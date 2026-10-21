-- ============================================================================
-- HR demo dataset — deterministic seed (MySQL 8.0+, 4x scale: ~160 employees)
-- ============================================================================
-- MySQL port of samples/hr/02_seed.sql.  Do NOT modify the Postgres originals.
--
-- All data is generated from id-arithmetic and fixed arrays (NO random()), so
-- two fresh loads produce byte-identical state.  Integrity SELECT checks at the
-- end print PASS/FAIL for each SCD natural-key invariant.
--
-- Postgres → MySQL translation notes:
--   • generate_series(a,b)   → INSERT INTO t ... WITH RECURSIVE cte(n) AS (...) SELECT ...
--   • ARRAY[…][i]            → ELT(i, …)
--   • expr || expr           → CONCAT(expr, expr)
--   • lpad(x::text, n, '0') → LPAD(x, n, '0')
--   • date + integer         → DATE_ADD(date, INTERVAL integer DAY)
--   • CROSS JOIN LATERAL     → inline expression (no lateral subquery needed)
--   • UPDATE … FROM          → UPDATE … JOIN … SET
--   • DO $$ … $$ plpgsql     → SELECT IF(…, 'PASS', 'FAIL') checks
--   • true / false           → 1 / 0
--   • ::numeric / ::text     → removed (MySQL implicit conversion)
--   • WITH ... INSERT INTO   → INSERT INTO ... WITH ...  (MySQL requires CTE after INSERT INTO)
--
-- Run AFTER 01_schema.sql.
-- ============================================================================

-- Disable FK checks for bulk insert: self-referencing department hierarchy and
-- employee manager_id are loaded in one shot; re-enabled before integrity checks.
SET FOREIGN_KEY_CHECKS = 0;

-- ── hr_core.department (12) ────────────────────────────────────────────────
INSERT INTO hr_core.department (department_id, dept_name, cost_center, parent_department_id)
WITH RECURSIVE gen(g) AS (SELECT 1 UNION ALL SELECT g+1 FROM gen WHERE g<12)
SELECT g,
       ELT(g, 'Executive','Engineering','Sales','Marketing','Finance','People',
               'Operations','Legal','Product','Support','IT','Data'),
       CONCAT('CC', LPAD(g, 3, '0')),
       CASE WHEN g <= 3 THEN NULL ELSE (g % 3) + 1 END
FROM gen;

-- ── hr_core.job (16) ───────────────────────────────────────────────────────
INSERT INTO hr_core.job (job_id, job_code, job_title, job_family, grade, min_salary, max_salary)
WITH RECURSIVE gen(g) AS (SELECT 1 UNION ALL SELECT g+1 FROM gen WHERE g<16)
SELECT g,
       CONCAT('J', LPAD(g, 3, '0')),
       ELT(g, 'Analyst','Senior Analyst','Engineer','Senior Engineer','Manager',
               'Senior Manager','Director','VP','Specialist','Lead','Architect',
               'Coordinator','Associate','Principal','Consultant','Head'),
       ELT(g, 'Engineering','Engineering','Sales','Marketing','Finance','People',
               'Operations','Legal','Product','Support','IT','Data','Engineering',
               'Product','Sales','Operations'),
       ((g - 1) % 8) + 1,
       40000 + (((g - 1) % 8) + 1) * 14000,
       40000 + (((g - 1) % 8) + 1) * 14000 + 45000
FROM gen;

-- ── hr_core.location (8) ───────────────────────────────────────────────────
INSERT INTO hr_core.location (location_id, site_name, city, country_code, region)
WITH RECURSIVE gen(g) AS (SELECT 1 UNION ALL SELECT g+1 FROM gen WHERE g<8)
SELECT g,
       ELT(g, 'HQ','East Campus','West Campus','EMEA Hub','APAC Hub',
               'North Office','South Office','Remote'),
       ELT(g, 'New York','Boston','San Francisco','London','Singapore',
               'Chicago','Austin','Remote'),
       ELT(g, 'US','US','US','GB','SG','US','US','US'),
       ELT(g, 'NA','NA','NA','EMEA','APAC','NA','NA','NA')
FROM gen;

-- ── hr_core.employee (160) ─────────────────────────────────────────────────
INSERT INTO hr_core.employee
    (employee_id, first_name, last_name, email, date_of_birth, gender,
     national_id_last4, hire_date, department_id, manager_id, employment_status)
WITH RECURSIVE gen(e) AS (SELECT 1 UNION ALL SELECT e+1 FROM gen WHERE e<160)
SELECT e,
       ELT(((e-1) % 20)+1, 'Ava','Liam','Noah','Emma','Olivia','Ethan','Mia','Lucas','Sophia','Mason',
                            'Isabella','Logan','Amelia','James','Harper','Aiden','Evelyn','Jack','Abigail','Henry'),
       ELT(((e*3-1) % 20)+1, 'Smith','Johnson','Williams','Brown','Jones','Garcia','Miller','Davis','Rodriguez','Martinez',
                              'Hernandez','Lopez','Gonzalez','Wilson','Anderson','Thomas','Taylor','Moore','Jackson','Martin'),
       LOWER(CONCAT(
           ELT(((e-1) % 20)+1, 'ava','liam','noah','emma','olivia','ethan','mia','lucas','sophia','mason',
                                'isabella','logan','amelia','james','harper','aiden','evelyn','jack','abigail','henry'),
           '.',
           ELT(((e*3-1) % 20)+1, 'smith','johnson','williams','brown','jones','garcia','miller','davis','rodriguez','martinez',
                                  'hernandez','lopez','gonzalez','wilson','anderson','thomas','taylor','moore','jackson','martin'),
           e, '@example.com')),
       DATE_ADD('1970-01-01', INTERVAL (e * 97) % 9000 DAY),
       CASE WHEN e % 7 = 0 THEN 'X' WHEN e % 2 = 0 THEN 'F' ELSE 'M' END,
       LPAD((e * 37) % 10000, 4, '0'),
       DATE_ADD('2015-01-01', INTERVAL (e * 53) % 3200 DAY),
       ((e - 1) % 12) + 1,
       CASE WHEN e <= 12 THEN NULL ELSE (e % 12) + 1 END,
       'active'
FROM gen;

-- ── hr_core.job_assignment_history (1–3 per employee) ──────────────────────
INSERT INTO hr_core.job_assignment_history
    (employee_id, job_id, department_id, location_id, lvl, assignment_type,
     effective_from, effective_to, is_current)
WITH RECURSIVE s_gen(s) AS (SELECT 0 UNION ALL SELECT s+1 FROM s_gen WHERE s<2)
SELECT e.employee_id,
       ((e.employee_id + s_gen.s) % 16) + 1,
       ((e.employee_id + s_gen.s) % 12) + 1,
       ((e.employee_id + s_gen.s) % 8)  + 1,
       ((e.employee_id + s_gen.s) % 8)  + 1,   -- lvl (cryptic)
       CASE WHEN s_gen.s = 0 THEN 'hire'
            ELSE ELT(((e.employee_id + s_gen.s) % 3) + 1, 'promotion', 'transfer', 'reorg')
       END,
       DATE_ADD(e.hire_date, INTERVAL s_gen.s * 400 DAY),
       CASE WHEN s_gen.s = (e.employee_id % 3) THEN NULL
            ELSE DATE_ADD(e.hire_date, INTERVAL (s_gen.s + 1) * 400 DAY)
       END,
       (s_gen.s = e.employee_id % 3)
FROM hr_core.employee e
CROSS JOIN s_gen
WHERE s_gen.s <= (e.employee_id % 3);

-- sync the current home department onto the employee master
UPDATE hr_core.employee e
JOIN hr_core.job_assignment_history jah
    ON jah.employee_id = e.employee_id AND jah.is_current = 1
SET e.department_id = jah.department_id;

-- ── hr_core.employment_status_history ──────────────────────────────────────
-- base "active" row for everyone; closed off for those who later change state
INSERT INTO hr_core.employment_status_history
    (employee_id, employment_status, status_reason, effective_from, effective_to, is_current)
SELECT e.employee_id, 'active', NULL,
       e.hire_date,
       CASE WHEN e.employee_id % 13 = 0 OR e.employee_id % 17 = 0
            THEN DATE_ADD(e.hire_date, INTERVAL 1500 DAY) ELSE NULL END,
       NOT (e.employee_id % 13 = 0 OR e.employee_id % 17 = 0)
FROM hr_core.employee e;

-- terminated (every 13th)
INSERT INTO hr_core.employment_status_history
    (employee_id, employment_status, status_reason, effective_from, effective_to, is_current)
SELECT e.employee_id, 'terminated', 'voluntary',
       DATE_ADD(e.hire_date, INTERVAL 1500 DAY), NULL, 1
FROM hr_core.employee e
WHERE e.employee_id % 13 = 0;

-- on_leave (every 17th, not already terminated)
INSERT INTO hr_core.employment_status_history
    (employee_id, employment_status, status_reason, effective_from, effective_to, is_current)
SELECT e.employee_id, 'on_leave', 'medical',
       DATE_ADD(e.hire_date, INTERVAL 1500 DAY), NULL, 1
FROM hr_core.employee e
WHERE e.employee_id % 17 = 0 AND e.employee_id % 13 <> 0;

-- sync current status onto the employee master
UPDATE hr_core.employee e
JOIN hr_core.employment_status_history esh
    ON esh.employee_id = e.employee_id AND esh.is_current = 1
SET e.employment_status = esh.employment_status;

-- ── hr_core.performance_review (3 cycles, non-terminated employees) ─────────
INSERT INTO hr_core.performance_review
    (employee_id, review_cycle, rating, reviewer_id, review_date)
SELECT e.employee_id, c.cycle,
       ((e.employee_id + c.idx) % 5) + 1,
       e.manager_id,
       c.rdate
FROM hr_core.employee e
JOIN (SELECT '2023-H2' AS cycle, 1 AS idx, '2023-12-15' AS rdate
      UNION ALL SELECT '2024-H1', 2, '2024-06-15'
      UNION ALL SELECT '2024-H2', 3, '2024-12-15') AS c
WHERE e.employment_status <> 'terminated';

-- ===========================================================================
-- hr_comp — Compensation / Payroll
-- ===========================================================================

-- ── hr_comp.pay_employee (160, shared key) ─────────────────────────────────
INSERT INTO hr_comp.pay_employee (employee_id, pay_group, payroll_no, pay_frequency)
WITH RECURSIVE gen(e) AS (SELECT 1 UNION ALL SELECT e+1 FROM gen WHERE e<160)
SELECT e,
       ELT(((e-1) % 3)+1, 'BW-US','SM-US','M-EU'),
       CONCAT('P', LPAD(e, 5, '0')),
       ELT(((e-1) % 3)+1, 'biweekly','semimonthly','monthly')
FROM gen;

-- ── hr_comp.benefit_plan (10) ──────────────────────────────────────────────
INSERT INTO hr_comp.benefit_plan (plan_code, plan_name, plan_type, tier)
WITH RECURSIVE gen(g) AS (SELECT 1 UNION ALL SELECT g+1 FROM gen WHERE g<10)
SELECT CONCAT('BP', LPAD(g, 2, '0')),
       ELT(g, 'Basic Health','Plus Health','Premium Health','Basic Dental','Premium Dental',
               'Vision','401k Standard','401k Plus','Life 1x','Life 2x'),
       ELT(g, 'health','health','health','dental','dental',
               'vision','retirement','retirement','life','life'),
       ELT(g, 'basic','plus','premium','basic','premium',
               'standard','standard','plus','1x','2x')
FROM gen;

-- ── hr_comp.salary_history (2–4 per employee) ──────────────────────────────
-- Postgres used CROSS JOIN LATERAL to factor out the base/raise sub-expressions.
-- MySQL inlines them directly (identical arithmetic results).
INSERT INTO hr_comp.salary_history
    (employee_id, amt, currency_code, rsn, prior_amt, effective_from, effective_to, is_current)
WITH RECURSIVE s_gen(s) AS (SELECT 0 UNION ALL SELECT s+1 FROM s_gen WHERE s<3)
SELECT pe.employee_id,
       -- amt (cryptic): base + s * raise, base = 50000 + (eid%20)*4000, raise = 4000 + (eid%5)*1500
       (50000 + (pe.employee_id % 20) * 4000) + s_gen.s * (4000 + (pe.employee_id % 5) * 1500),
       CASE WHEN pe.employee_id % 11 = 0 THEN 'EUR'
            WHEN pe.employee_id % 19 = 0 THEN 'GBP' ELSE 'USD' END,
       -- rsn (cryptic)
       CASE WHEN s_gen.s = 0 THEN 'ADJ'
            ELSE ELT(((pe.employee_id + s_gen.s) % 4) + 1, 'MERIT','PROMO','MKT','ADJ')
       END,
       CASE WHEN s_gen.s = 0 THEN NULL
            ELSE (50000 + (pe.employee_id % 20) * 4000) + (s_gen.s-1) * (4000 + (pe.employee_id % 5) * 1500)
       END,
       DATE_ADD(emp.hire_date, INTERVAL s_gen.s * 365 DAY),
       CASE WHEN s_gen.s = (1 + pe.employee_id % 3) THEN NULL
            ELSE DATE_ADD(emp.hire_date, INTERVAL (s_gen.s + 1) * 365 DAY)
       END,
       (s_gen.s = (1 + pe.employee_id % 3))
FROM hr_comp.pay_employee pe
JOIN hr_core.employee emp ON emp.employee_id = pe.employee_id
CROSS JOIN s_gen
WHERE s_gen.s <= (1 + pe.employee_id % 3);

-- ── hr_comp.benefit_enrollment (1–3 plans per employee) ────────────────────
INSERT INTO hr_comp.benefit_enrollment
    (employee_id, plan_code, coverage_level, enrolled_from, enrolled_to, is_current)
WITH RECURSIVE s_gen(s) AS (SELECT 0 UNION ALL SELECT s+1 FROM s_gen WHERE s<2)
SELECT pe.employee_id,
       CONCAT('BP', LPAD(((pe.employee_id + s_gen.s) % 10) + 1, 2, '0')),
       ELT(((pe.employee_id + s_gen.s) % 3) + 1, 'employee','employee+1','family'),
       DATE_ADD(emp.hire_date, INTERVAL 30 DAY),
       NULL, 1
FROM hr_comp.pay_employee pe
JOIN hr_core.employee emp ON emp.employee_id = pe.employee_id
CROSS JOIN s_gen
WHERE s_gen.s <= (pe.employee_id % 3);

-- second SCD-2 pattern: a superseded prior enrollment for every 7th employee
-- (their first plan changed coverage). FIRST push the current row's start date
-- forward, THEN insert the prior row at the original start — order matters, so
-- the two windows never collide on (employee, plan, enrolled_from).
UPDATE hr_comp.benefit_enrollment be
JOIN hr_core.employee emp ON emp.employee_id = be.employee_id
SET be.enrolled_from = DATE_ADD(emp.hire_date, INTERVAL 430 DAY)
WHERE be.employee_id % 7 = 0
  AND be.is_current = 1
  AND be.plan_code = CONCAT('BP', LPAD((be.employee_id % 10) + 1, 2, '0'));

INSERT INTO hr_comp.benefit_enrollment
    (employee_id, plan_code, coverage_level, enrolled_from, enrolled_to, is_current)
SELECT pe.employee_id,
       CONCAT('BP', LPAD((pe.employee_id % 10) + 1, 2, '0')),
       'employee',
       DATE_ADD(emp.hire_date, INTERVAL 30 DAY),
       DATE_ADD(emp.hire_date, INTERVAL 430 DAY),
       0
FROM hr_comp.pay_employee pe
JOIN hr_core.employee emp ON emp.employee_id = pe.employee_id
WHERE pe.employee_id % 7 = 0;

-- ===========================================================================
-- Re-enable FK checks and verify integrity
-- ===========================================================================

SET FOREIGN_KEY_CHECKS = 1;

-- ===========================================================================
-- Integrity checks — MySQL replacement for the Postgres DO $$ … $$ block.
-- Every SELECT below should return result = 'PASS'.
-- ===========================================================================

SELECT 'salary_history_current' AS check_name,
       IF(COUNT(*) = 0, 'PASS',
          CONCAT('FAIL: ', COUNT(*), ' employees without exactly one current row')) AS result
FROM (
    SELECT employee_id FROM hr_comp.salary_history WHERE is_current = 1
    GROUP BY employee_id HAVING COUNT(*) <> 1) x;

SELECT 'job_assignment_history_current' AS check_name,
       IF(COUNT(*) = 0, 'PASS',
          CONCAT('FAIL: ', COUNT(*), ' employees without exactly one current row')) AS result
FROM (
    SELECT employee_id FROM hr_core.job_assignment_history WHERE is_current = 1
    GROUP BY employee_id HAVING COUNT(*) <> 1) x;

SELECT 'employment_status_history_current' AS check_name,
       IF(COUNT(*) = 0, 'PASS',
          CONCAT('FAIL: ', COUNT(*), ' employees without exactly one current row')) AS result
FROM (
    SELECT employee_id FROM hr_core.employment_status_history WHERE is_current = 1
    GROUP BY employee_id HAVING COUNT(*) <> 1) x;

SELECT 'benefit_enrollment_current' AS check_name,
       IF(COUNT(*) = 0, 'PASS',
          CONCAT('FAIL: ', COUNT(*), ' (employee,plan) without exactly one current row')) AS result
FROM (
    SELECT employee_id, plan_code FROM hr_comp.benefit_enrollment WHERE is_current = 1
    GROUP BY employee_id, plan_code HAVING COUNT(*) <> 1) x;

SELECT 'pay_employee_cross_reference' AS check_name,
       IF(COUNT(*) = 0, 'PASS',
          CONCAT('FAIL: ', COUNT(*), ' pay_employee rows have no matching hr_core.employee')) AS result
FROM hr_comp.pay_employee pe
LEFT JOIN hr_core.employee e ON e.employee_id = pe.employee_id
WHERE e.employee_id IS NULL;
