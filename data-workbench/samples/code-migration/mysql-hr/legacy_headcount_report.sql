-- ============================================================================
-- Legacy HR headcount & compensation report  (MySQL 8.0)
-- Source: hr-mysql sample  (schemas: hr_core, hr_comp)
--
-- Produces, per department, the current headcount, the list of current job
-- titles held, and the average current annual base salary. "Current" is the
-- SCD-2 as-of view (is_current = 1). This is intentionally written in idiomatic
-- legacy MySQL so a code migration has real constructs to convert.
-- ============================================================================

SELECT
    d.`department_id`,
    d.`dept_name`,
    d.`cost_center`,
    COUNT(DISTINCT e.`employee_id`)                              AS headcount,
    -- MySQL string aggregation; note the implicit group_concat_max_len truncation.
    GROUP_CONCAT(DISTINCT j.`job_title` ORDER BY j.`job_title` SEPARATOR ', ') AS current_titles,
    -- hr_comp.salary_history.amt is a cryptic column = annualized base salary.
    ROUND(AVG(IFNULL(sh.`amt`, 0)), 2)                          AS avg_annual_base,
    -- DATE_FORMAT with %-style tokens is MySQL-specific.
    DATE_FORMAT(MAX(e.`hire_date`), '%Y-%m-%d')                 AS most_recent_hire
FROM hr_core.`employee` e
-- No cross-schema FK — hr_core and hr_comp join on the shared business key.
JOIN hr_core.`department` d
    ON d.`department_id` = e.`department_id`
-- SCD-2 as-of join: the employee's CURRENT job assignment only.
JOIN hr_core.`job_assignment_history` jah
    ON jah.`employee_id` = e.`employee_id`
   AND jah.`is_current` = 1               -- TINYINT(1) used as a boolean
JOIN hr_core.`job` j
    ON j.`job_id` = jah.`job_id`
-- Cross-schema join to compensation on the shared employee_id business key.
LEFT JOIN hr_comp.`salary_history` sh
    ON sh.`employee_id` = e.`employee_id`
   AND sh.`is_current` = 1
   AND sh.`rsn` IN ('MERIT', 'PROMO', 'MKT', 'ADJ')   -- cryptic rsn = change reason
WHERE e.`employment_status` = 'active'
  -- MySQL date math idiom.
  AND e.`hire_date` <= DATE_SUB(CURDATE(), INTERVAL 90 DAY)
GROUP BY d.`department_id`, d.`dept_name`, d.`cost_center`
HAVING headcount > 0
ORDER BY headcount DESC;
