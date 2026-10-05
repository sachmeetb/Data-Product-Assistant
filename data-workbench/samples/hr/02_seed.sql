-- ============================================================================
-- HR demo dataset — deterministic seed (4x scale: ~160 employees)
-- ============================================================================
-- All data is generated from id-arithmetic and fixed arrays (NO random()), so
-- two fresh loads produce byte-identical state. An integrity DO-block at the end
-- raises an exception if any SCD natural key lacks exactly one current row or the
-- shared employee_id key is misaligned across the two systems.
--
-- Run AFTER 01_schema.sql.
-- ============================================================================

-- ── hr_core.department (12) ────────────────────────────────────────────────
INSERT INTO hr_core.department (department_id, dept_name, cost_center, parent_department_id)
SELECT g,
       (ARRAY['Executive','Engineering','Sales','Marketing','Finance','People',
              'Operations','Legal','Product','Support','IT','Data'])[g],
       'CC' || lpad(g::text, 3, '0'),
       CASE WHEN g <= 3 THEN NULL ELSE ((g % 3) + 1) END
FROM generate_series(1, 12) AS g;

-- ── hr_core.job (16) ───────────────────────────────────────────────────────
INSERT INTO hr_core.job (job_id, job_code, job_title, job_family, grade, min_salary, max_salary)
SELECT g,
       'J' || lpad(g::text, 3, '0'),
       (ARRAY['Analyst','Senior Analyst','Engineer','Senior Engineer','Manager',
              'Senior Manager','Director','VP','Specialist','Lead','Architect',
              'Coordinator','Associate','Principal','Consultant','Head'])[g],
       (ARRAY['Engineering','Engineering','Sales','Marketing','Finance','People',
              'Operations','Legal','Product','Support','IT','Data','Engineering',
              'Product','Sales','Operations'])[g],
       ((g - 1) % 8) + 1,
       40000 + (((g - 1) % 8) + 1) * 14000,
       40000 + (((g - 1) % 8) + 1) * 14000 + 45000
FROM generate_series(1, 16) AS g;

-- ── hr_core.location (8) ───────────────────────────────────────────────────
INSERT INTO hr_core.location (location_id, site_name, city, country_code, region)
SELECT g,
       (ARRAY['HQ','East Campus','West Campus','EMEA Hub','APAC Hub',
              'North Office','South Office','Remote'])[g],
       (ARRAY['New York','Boston','San Francisco','London','Singapore',
              'Chicago','Austin','Remote'])[g],
       (ARRAY['US','US','US','GB','SG','US','US','US'])[g],
       (ARRAY['NA','NA','NA','EMEA','APAC','NA','NA','NA'])[g]
FROM generate_series(1, 8) AS g;

-- ── hr_core.employee (160) ─────────────────────────────────────────────────
INSERT INTO hr_core.employee
    (employee_id, first_name, last_name, email, date_of_birth, gender,
     national_id_last4, hire_date, department_id, manager_id, employment_status)
SELECT e,
       (ARRAY['Ava','Liam','Noah','Emma','Olivia','Ethan','Mia','Lucas','Sophia','Mason',
              'Isabella','Logan','Amelia','James','Harper','Aiden','Evelyn','Jack','Abigail','Henry'])[((e - 1) % 20) + 1],
       (ARRAY['Smith','Johnson','Williams','Brown','Jones','Garcia','Miller','Davis','Rodriguez','Martinez',
              'Hernandez','Lopez','Gonzalez','Wilson','Anderson','Thomas','Taylor','Moore','Jackson','Martin'])[((e * 3 - 1) % 20) + 1],
       lower((ARRAY['ava','liam','noah','emma','olivia','ethan','mia','lucas','sophia','mason',
                    'isabella','logan','amelia','james','harper','aiden','evelyn','jack','abigail','henry'])[((e - 1) % 20) + 1]
             || '.' ||
             (ARRAY['smith','johnson','williams','brown','jones','garcia','miller','davis','rodriguez','martinez',
                    'hernandez','lopez','gonzalez','wilson','anderson','thomas','taylor','moore','jackson','martin'])[((e * 3 - 1) % 20) + 1]
             || e::text || '@example.com'),
       DATE '1970-01-01' + ((e * 97) % 9000),
       CASE WHEN e % 7 = 0 THEN 'X' WHEN e % 2 = 0 THEN 'F' ELSE 'M' END,
       lpad(((e * 37) % 10000)::text, 4, '0'),
       DATE '2015-01-01' + ((e * 53) % 3200),
       ((e - 1) % 12) + 1,
       CASE WHEN e <= 12 THEN NULL ELSE ((e % 12) + 1) END,
       'active'
FROM generate_series(1, 160) AS e;

-- ── hr_core.job_assignment_history (1–3 per employee) ──────────────────────
INSERT INTO hr_core.job_assignment_history
    (employee_id, job_id, department_id, location_id, lvl, assignment_type,
     effective_from, effective_to, is_current)
SELECT e.employee_id,
       ((e.employee_id + s) % 16) + 1,
       ((e.employee_id + s) % 12) + 1,
       ((e.employee_id + s) % 8) + 1,
       ((e.employee_id + s) % 8) + 1,                                   -- lvl (cryptic)
       CASE WHEN s = 0 THEN 'hire'
            ELSE (ARRAY['promotion','transfer','reorg'])[((e.employee_id + s) % 3) + 1] END,
       e.hire_date + (s * 400),
       CASE WHEN s = (e.employee_id % 3) THEN NULL
            ELSE e.hire_date + ((s + 1) * 400) END,
       (s = (e.employee_id % 3))
FROM hr_core.employee e
CROSS JOIN generate_series(0, 2) AS s
WHERE s <= (e.employee_id % 3);

-- sync the current home department onto the employee master
UPDATE hr_core.employee e
SET department_id = jah.department_id
FROM hr_core.job_assignment_history jah
WHERE jah.employee_id = e.employee_id AND jah.is_current;

-- ── hr_core.employment_status_history ──────────────────────────────────────
-- base "active" row for everyone; closed off for those who later change state
INSERT INTO hr_core.employment_status_history
    (employee_id, employment_status, status_reason, effective_from, effective_to, is_current)
SELECT e.employee_id, 'active', NULL,
       e.hire_date,
       CASE WHEN e.employee_id % 13 = 0 OR e.employee_id % 17 = 0
            THEN e.hire_date + 1500 ELSE NULL END,
       NOT (e.employee_id % 13 = 0 OR e.employee_id % 17 = 0)
FROM hr_core.employee e;

-- terminated (every 13th)
INSERT INTO hr_core.employment_status_history
    (employee_id, employment_status, status_reason, effective_from, effective_to, is_current)
SELECT e.employee_id, 'terminated', 'voluntary', e.hire_date + 1500, NULL, true
FROM hr_core.employee e
WHERE e.employee_id % 13 = 0;

-- on_leave (every 17th, not already terminated)
INSERT INTO hr_core.employment_status_history
    (employee_id, employment_status, status_reason, effective_from, effective_to, is_current)
SELECT e.employee_id, 'on_leave', 'medical', e.hire_date + 1500, NULL, true
FROM hr_core.employee e
WHERE e.employee_id % 17 = 0 AND e.employee_id % 13 <> 0;

-- sync current status onto the employee master
UPDATE hr_core.employee e
SET employment_status = esh.employment_status
FROM hr_core.employment_status_history esh
WHERE esh.employee_id = e.employee_id AND esh.is_current;

-- ── hr_core.performance_review (3 cycles, non-terminated employees) ─────────
INSERT INTO hr_core.performance_review
    (employee_id, review_cycle, rating, reviewer_id, review_date)
SELECT e.employee_id, c.cycle,
       ((e.employee_id + c.idx) % 5) + 1,
       e.manager_id,
       c.rdate
FROM hr_core.employee e
CROSS JOIN (VALUES ('2023-H2', 1, DATE '2023-12-15'),
                   ('2024-H1', 2, DATE '2024-06-15'),
                   ('2024-H2', 3, DATE '2024-12-15')) AS c(cycle, idx, rdate)
WHERE e.employment_status <> 'terminated';

-- ===========================================================================
-- hr_comp — Compensation / Payroll
-- ===========================================================================

-- ── hr_comp.pay_employee (160, shared key) ─────────────────────────────────
INSERT INTO hr_comp.pay_employee (employee_id, pay_group, payroll_no, pay_frequency)
SELECT e,
       (ARRAY['BW-US','SM-US','M-EU'])[((e - 1) % 3) + 1],
       'P' || lpad(e::text, 5, '0'),
       (ARRAY['biweekly','semimonthly','monthly'])[((e - 1) % 3) + 1]
FROM generate_series(1, 160) AS e;

-- ── hr_comp.benefit_plan (10) ──────────────────────────────────────────────
INSERT INTO hr_comp.benefit_plan (plan_code, plan_name, plan_type, tier)
SELECT 'BP' || lpad(g::text, 2, '0'),
       (ARRAY['Basic Health','Plus Health','Premium Health','Basic Dental','Premium Dental',
              'Vision','401k Standard','401k Plus','Life 1x','Life 2x'])[g],
       (ARRAY['health','health','health','dental','dental',
              'vision','retirement','retirement','life','life'])[g],
       (ARRAY['basic','plus','premium','basic','premium',
              'standard','standard','plus','1x','2x'])[g]
FROM generate_series(1, 10) AS g;

-- ── hr_comp.salary_history (2–4 per employee) ──────────────────────────────
INSERT INTO hr_comp.salary_history
    (employee_id, amt, currency_code, rsn, prior_amt, effective_from, effective_to, is_current)
SELECT pe.employee_id,
       calc.base + s * calc.raise,                                      -- amt (cryptic)
       CASE WHEN pe.employee_id % 11 = 0 THEN 'EUR'
            WHEN pe.employee_id % 19 = 0 THEN 'GBP' ELSE 'USD' END,
       CASE WHEN s = 0 THEN 'ADJ'
            ELSE (ARRAY['MERIT','PROMO','MKT','ADJ'])[((pe.employee_id + s) % 4) + 1] END,  -- rsn (cryptic)
       CASE WHEN s = 0 THEN NULL ELSE calc.base + (s - 1) * calc.raise END,
       emp.hire_date + (s * 365),
       CASE WHEN s = (1 + pe.employee_id % 3) THEN NULL
            ELSE emp.hire_date + ((s + 1) * 365) END,
       (s = (1 + pe.employee_id % 3))
FROM hr_comp.pay_employee pe
JOIN hr_core.employee emp ON emp.employee_id = pe.employee_id
CROSS JOIN generate_series(0, 3) AS s
CROSS JOIN LATERAL (
    SELECT (50000 + ((pe.employee_id % 20) * 4000))::numeric(12,2) AS base,
           (4000 + (pe.employee_id % 5) * 1500)::numeric(12,2)     AS raise
) AS calc
WHERE s <= (1 + pe.employee_id % 3);

-- ── hr_comp.benefit_enrollment (1–3 plans per employee) ────────────────────
INSERT INTO hr_comp.benefit_enrollment
    (employee_id, plan_code, coverage_level, enrolled_from, enrolled_to, is_current)
SELECT pe.employee_id,
       'BP' || lpad((((pe.employee_id + s) % 10) + 1)::text, 2, '0'),
       (ARRAY['employee','employee+1','family'])[((pe.employee_id + s) % 3) + 1],
       emp.hire_date + 30,
       NULL, true
FROM hr_comp.pay_employee pe
JOIN hr_core.employee emp ON emp.employee_id = pe.employee_id
CROSS JOIN generate_series(0, 2) AS s
WHERE s <= (pe.employee_id % 3);

-- second SCD-2 pattern: a superseded prior enrollment for every 7th employee
-- (their first plan changed coverage). FIRST push the current row's start date
-- forward, THEN insert the prior row at the original start — order matters, so
-- the two windows never collide on (employee, plan, enrolled_from).
UPDATE hr_comp.benefit_enrollment be
SET enrolled_from = (SELECT emp.hire_date + 430
                     FROM hr_core.employee emp
                     WHERE emp.employee_id = be.employee_id)
WHERE be.employee_id % 7 = 0
  AND be.is_current
  AND be.plan_code = 'BP' || lpad(((be.employee_id % 10) + 1)::text, 2, '0');

INSERT INTO hr_comp.benefit_enrollment
    (employee_id, plan_code, coverage_level, enrolled_from, enrolled_to, is_current)
SELECT pe.employee_id,
       'BP' || lpad(((pe.employee_id % 10) + 1)::text, 2, '0'),
       'employee',
       emp.hire_date + 30,
       emp.hire_date + 430,
       false
FROM hr_comp.pay_employee pe
JOIN hr_core.employee emp ON emp.employee_id = pe.employee_id
WHERE pe.employee_id % 7 = 0;

-- ===========================================================================
-- Integrity checks — fail loudly if the deterministic generation drifted
-- ===========================================================================
DO $$
DECLARE bad integer;
BEGIN
    SELECT count(*) INTO bad FROM (
        SELECT employee_id FROM hr_comp.salary_history WHERE is_current
        GROUP BY employee_id HAVING count(*) <> 1) x;
    IF bad > 0 THEN RAISE EXCEPTION 'salary_history: % employees without exactly one current row', bad; END IF;

    SELECT count(*) INTO bad FROM (
        SELECT employee_id FROM hr_core.job_assignment_history WHERE is_current
        GROUP BY employee_id HAVING count(*) <> 1) x;
    IF bad > 0 THEN RAISE EXCEPTION 'job_assignment_history: % employees without exactly one current row', bad; END IF;

    SELECT count(*) INTO bad FROM (
        SELECT employee_id FROM hr_core.employment_status_history WHERE is_current
        GROUP BY employee_id HAVING count(*) <> 1) x;
    IF bad > 0 THEN RAISE EXCEPTION 'employment_status_history: % employees without exactly one current row', bad; END IF;

    SELECT count(*) INTO bad FROM (
        SELECT employee_id, plan_code FROM hr_comp.benefit_enrollment WHERE is_current
        GROUP BY employee_id, plan_code HAVING count(*) <> 1) x;
    IF bad > 0 THEN RAISE EXCEPTION 'benefit_enrollment: % (employee,plan) without exactly one current row', bad; END IF;

    SELECT count(*) INTO bad
    FROM hr_comp.pay_employee pe
    LEFT JOIN hr_core.employee e ON e.employee_id = pe.employee_id
    WHERE e.employee_id IS NULL;
    IF bad > 0 THEN RAISE EXCEPTION '% pay_employee rows have no matching hr_core.employee', bad; END IF;

    RAISE NOTICE 'HR seed integrity checks passed.';
END $$;
