# Human Resources Domain Guidance

Additional domain rules for HR data that supplement the structured patterns in `hr.yaml`.

## Employment constraints

- The employees database represents a company founded in 1985. No hire dates should precede 1985-01-01.
- Departments have been stable since founding. Department numbers are in the range d001-d009.

## Title and role conventions

- Employee titles in this dataset use a fixed set: "Senior Engineer", "Staff", "Engineer", "Senior Staff", "Assistant Engineer", "Technique Leader", "Manager". Other values are likely data errors.

## Salary business rules

- No employee should have a salary less than minimum wage equivalent ($15,000/year as of the dataset period).
- Salary from_date should always be on or after the employee's hire_date.
