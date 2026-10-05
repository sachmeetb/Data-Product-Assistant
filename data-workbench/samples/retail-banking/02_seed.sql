-- =============================================================================
-- Data Workbench sample dataset — Retail Banking (brownfield estate)
-- File 2 of 3: seed data
--
-- Strategy: everything is generated deterministically (id-arithmetic +
-- generate_series, NO random()) so reloads are reproducible. Transaction
-- timestamps/dates that carry a recency signal are anchored to CURRENT_DATE;
-- everything else uses fixed dates. Reloads are identical within a day.
--
-- Referential integrity:
--   * Every derived customer_id lands in 1..200, so it always resolves against
--     crm.party (there are no cross-schema FKs — this is a data-level guarantee,
--     re-checked at the bottom).
--   * Intra-schema FKs (contact/kyc→party, balance/txn→account, limit/txn→card,
--     repayment/delinquency→loan, payment→beneficiary) are enforced by the DDL.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- crm.party — 200 master customer records
--   customer_segment: business when n % 12 = 0, private when n % 50 = 0,
--                     else mass ~60% / affluent ~30% / premium ~10%
--   kyc_status:       expired when n % 20 = 7, pending when n % 10 = 3,
--                     else verified
-- ---------------------------------------------------------------------------
INSERT INTO crm.party (customer_id, full_name, date_of_birth, gender, email,
                       mobile_number, residential_address, customer_segment,
                       kyc_status, onboarded_date)
SELECT
    n AS customer_id,
    nm.full_name,
    DATE '1960-01-01' + ((n * 7919) % 16000) AS date_of_birth,
    g.gender,
    lower(nm.fname) || '.' || lower(nm.lname) || n || '@example.in' AS email,
    '+91-9' || lpad(((n * 686593) % 1000000000)::text, 9, '0') AS mobile_number,
    ((n % 240) + 1)::text || ' ' ||
    (ARRAY['MG Road','Nehru Street','Gandhi Nagar Main Road','Link Road',
           'Station Road','Mount Road','Brigade Road','Park Street',
           'FC Road','Anna Salai','Ring Road','Rajpath Lane'])[((n * 3) % 12) + 1] || ', ' ||
    (ARRAY['Mumbai 400053','New Delhi 110001','Bengaluru 560034','Chennai 600017',
           'Hyderabad 500034','Pune 411007','Kolkata 700091','Ahmedabad 380015'])[((n * 7) % 8) + 1]
        AS residential_address,
    seg.customer_segment,
    CASE WHEN n % 20 = 7 THEN 'expired'
         WHEN n % 10 = 3 THEN 'pending'
         ELSE 'verified'
    END AS kyc_status,
    DATE '2015-01-06' + ((n * 7919) % 3800) AS onboarded_date
FROM generate_series(1, 200) AS n
CROSS JOIN LATERAL (
    SELECT CASE WHEN n % 50 = 0 THEN 'private'
                WHEN n % 12 = 0 THEN 'business'
                WHEN n % 10 <= 5 THEN 'mass'
                WHEN n % 10 <= 8 THEN 'affluent'
                ELSE 'premium'
           END AS customer_segment
) seg
CROSS JOIN LATERAL (
    SELECT CASE WHEN seg.customer_segment = 'business' THEN 'O'
                WHEN n % 2 = 0 THEN 'F'
                ELSE 'M'
           END AS gender
) g
CROSS JOIN LATERAL (
    SELECT
        (ARRAY['Sharma','Verma','Gupta','Mehta','Patel','Reddy','Nair','Iyer',
               'Menon','Kulkarni','Joshi','Chopra','Malhotra','Bhat','Rao',
               'Das','Mukherjee','Banerjee','Singh','Agarwal'])[((n * 7) % 20) + 1] AS lname,
        CASE WHEN g.gender = 'F'
             THEN (ARRAY['Priya','Ananya','Divya','Kavya','Sneha','Pooja','Neha',
                         'Anjali','Shreya','Ritu','Sunita','Meena','Lakshmi',
                         'Nandini','Swati','Rekha','Pallavi','Geeta','Isha','Aditi'])[((n * 5) % 20) + 1]
             ELSE (ARRAY['Aarav','Vihaan','Aditya','Arjun','Rohan','Karan','Rajesh',
                         'Suresh','Amit','Vikram','Sanjay','Manoj','Deepak','Nikhil',
                         'Rahul','Anil','Prakash','Harish','Sunil','Mohan'])[((n * 3) % 20) + 1]
        END AS fname
) nm0
CROSS JOIN LATERAL (
    SELECT nm0.fname, nm0.lname,
           CASE WHEN seg.customer_segment = 'business'
                THEN nm0.lname || ' ' ||
                     (ARRAY['Textiles Pvt Ltd','Traders Co','Industries Ltd',
                            'Exports Pvt Ltd','Infotech Pvt Ltd','Logistics Ltd'])[((n * 11) % 6) + 1]
                ELSE nm0.fname || ' ' || nm0.lname
           END AS full_name
) nm;

-- ---------------------------------------------------------------------------
-- crm.party_contact — 2 contacts per party (400 rows)
-- ---------------------------------------------------------------------------
INSERT INTO crm.party_contact (contact_id, customer_id, contact_type,
                               contact_value, is_primary)
SELECT
    p.customer_id * 10 + idx AS contact_id,
    p.customer_id,
    CASE idx WHEN 1 THEN 'email' ELSE 'mobile' END AS contact_type,
    CASE idx WHEN 1 THEN p.email ELSE p.mobile_number END AS contact_value,
    (idx = 1) AS is_primary
FROM crm.party p
CROSS JOIN generate_series(1, 2) AS idx;

-- ---------------------------------------------------------------------------
-- crm.kyc_verification — 1 verification per party (200 rows)
-- ---------------------------------------------------------------------------
INSERT INTO crm.kyc_verification (kyc_id, customer_id, document_type,
                                  kyc_status, verification_date)
SELECT
    p.customer_id AS kyc_id,
    p.customer_id,
    (ARRAY['passport','national_id','driving_licence','pan'])[((p.customer_id * 3) % 4) + 1] AS document_type,
    p.kyc_status,
    CASE p.kyc_status
        WHEN 'verified' THEN p.onboarded_date + ((p.customer_id * 7) % 30)::int
        WHEN 'expired'  THEN p.onboarded_date + 15
        ELSE NULL
    END AS verification_date
FROM crm.party p;

-- ---------------------------------------------------------------------------
-- core_banking.account — 300 deposit accounts
-- ---------------------------------------------------------------------------
INSERT INTO core_banking.account (account_id, customer_id, product_type,
                                  account_status, currency, open_date)
SELECT
    a AS account_id,
    ((a * 7 + 3) % 200) + 1 AS customer_id,
    (ARRAY['savings','current','overdraft','salary'])[((a * 3) % 4) + 1] AS product_type,
    CASE WHEN a % 37 = 0 THEN 'frozen'
         WHEN a % 29 = 0 THEN 'closed'
         WHEN a % 13 = 0 THEN 'dormant'
         ELSE 'active'
    END AS account_status,
    CASE WHEN a % 25 = 0 THEN 'EUR'
         WHEN a % 20 = 0 THEN 'USD'
         ELSE 'INR'
    END AS currency,
    DATE '2016-01-01' + ((a * 7919) % 3400) AS open_date
FROM generate_series(1, 300) AS a;

-- ---------------------------------------------------------------------------
-- core_banking.account_balance — current balance per account (300 rows)
--   available_balance <= ledger_balance by construction.
-- ---------------------------------------------------------------------------
INSERT INTO core_banking.account_balance (account_id, customer_id, balance_date,
                                          ledger_balance, available_balance)
SELECT
    acc.account_id,
    acc.customer_id,
    CURRENT_DATE AS balance_date,
    bal.ledger_balance,
    ROUND((bal.ledger_balance * (0.88 + ((acc.account_id % 10) / 100.0)))::numeric, 2) AS available_balance
FROM core_banking.account acc
CROSS JOIN LATERAL (
    SELECT ROUND((((acc.account_id * 2503 + 100000) % 900000 + 5000)
                  + ((acc.account_id * 7) % 100) / 100.0)::numeric, 2) AS ledger_balance
) bal;

-- ---------------------------------------------------------------------------
-- core_banking.deposit_transaction — ~4,000 rows over 300 accounts
--   per-account count 8..19; timestamps anchored to CURRENT_DATE
-- ---------------------------------------------------------------------------
INSERT INTO core_banking.deposit_transaction (txn_id, account_id, customer_id,
                                              amount, currency, txn_timestamp,
                                              debit_credit, channel)
SELECT
    acc.account_id * 100 + g.idx AS txn_id,
    acc.account_id,
    acc.customer_id,
    ROUND((((acc.account_id * 31 + g.idx * 7919) % 200000 + 100) / 100.0)::numeric, 2) AS amount,
    acc.currency,
    (CURRENT_DATE - ((acc.account_id * 7919 + g.idx * 104729) % 180)::int)::timestamp
        + (g.idx % 24) * INTERVAL '1 hour'
        + ((acc.account_id + g.idx) % 60)::int * INTERVAL '1 minute' AS txn_timestamp,
    CASE WHEN (acc.account_id * 3 + g.idx) % 10 < 6 THEN 'D' ELSE 'C' END AS debit_credit,
    (ARRAY['upi','upi','upi','pos','pos','online','online','atm','branch'])
        [((acc.account_id * 3 + g.idx * 7) % 9) + 1] AS channel
FROM core_banking.account acc
CROSS JOIN LATERAL generate_series(1, 8 + ((acc.account_id * 7) % 12)) AS g(idx);

-- ---------------------------------------------------------------------------
-- cards.card — 250 issued cards
--   debit cards carry credit_limit 0; others 50,000..500,000
-- ---------------------------------------------------------------------------
INSERT INTO cards.card (card_id, customer_id, card_type, card_status,
                        issue_date, expiry_date, credit_limit)
SELECT
    c AS card_id,
    ((c * 11 + 5) % 200) + 1 AS customer_id,
    ct.card_type,
    CASE WHEN c % 47 = 0 THEN 'hotlisted'
         WHEN c % 31 = 0 THEN 'expired'
         WHEN c % 23 = 0 THEN 'blocked'
         ELSE 'active'
    END AS card_status,
    DATE '2018-01-01' + ((c * 7919) % 2200) AS issue_date,
    DATE '2018-01-01' + ((c * 7919) % 2200) + 1460 AS expiry_date,
    CASE WHEN ct.card_type = 'debit' THEN 0
         ELSE (((c * 37) % 10) + 1) * 50000
    END AS credit_limit
FROM generate_series(1, 250) AS c
CROSS JOIN LATERAL (
    SELECT (ARRAY['classic','gold','platinum','corporate','debit'])[((c * 3) % 5) + 1] AS card_type
) ct;

-- ---------------------------------------------------------------------------
-- cards.card_limit — 2 limits per card (500 rows)
-- ---------------------------------------------------------------------------
INSERT INTO cards.card_limit (card_id, customer_id, limit_type, limit_amount)
SELECT
    cd.card_id,
    cd.customer_id,
    lt.limit_type,
    CASE lt.limit_type
        WHEN 'daily_pos' THEN (((cd.card_id * 13) % 8) + 2) * 25000
        ELSE                  (((cd.card_id * 7)  % 6) + 1) * 10000
    END AS limit_amount
FROM cards.card cd
CROSS JOIN (VALUES ('daily_pos'), ('daily_atm')) AS lt(limit_type);

-- ---------------------------------------------------------------------------
-- cards.card_transaction — ~2,500 rows over 250 cards
-- ---------------------------------------------------------------------------
INSERT INTO cards.card_transaction (card_txn_id, card_id, customer_id, amount,
                                    merchant_name, txn_date, txn_type)
SELECT
    cd.card_id * 100 + g.idx AS card_txn_id,
    cd.card_id,
    cd.customer_id,
    ROUND((((cd.card_id * 29 + g.idx * 104729) % 50000 + 50) / 100.0)::numeric, 2) AS amount,
    (ARRAY['Amazon','Flipkart','BigBazaar','Swiggy','Zomato','Reliance Fresh',
           'IRCTC','MakeMyTrip','Croma','Apollo Pharmacy','Shell','IndianOil'])
        [((cd.card_id + g.idx) % 12) + 1] AS merchant_name,
    (CURRENT_DATE - ((cd.card_id * 7919 + g.idx * 104729) % 180)::int) AS txn_date,
    (ARRAY['purchase','purchase','purchase','cash_advance','refund'])
        [((cd.card_id * 3 + g.idx) % 5) + 1] AS txn_type
FROM cards.card cd
CROSS JOIN LATERAL generate_series(1, 5 + ((cd.card_id * 7) % 12)) AS g(idx);

-- ---------------------------------------------------------------------------
-- lending.loan — 150 loans
-- ---------------------------------------------------------------------------
INSERT INTO lending.loan (loan_id, customer_id, loan_type, principal_amount,
                          interest_rate, disbursal_date, maturity_date, loan_status)
SELECT
    l AS loan_id,
    ((l * 13 + 7) % 200) + 1 AS customer_id,
    lt.loan_type,
    lt.principal_amount,
    ROUND((7.5 + ((l * 3) % 80) / 10.0)::numeric, 3) AS interest_rate,
    DATE '2019-01-01' + ((l * 7919) % 2000) AS disbursal_date,
    DATE '2019-01-01' + ((l * 7919) % 2000) + lt.term_days AS maturity_date,
    CASE WHEN l % 50 = 0 THEN 'written_off'
         WHEN l % 7  = 0 THEN 'delinquent'
         WHEN l % 9  = 0 THEN 'closed'
         ELSE 'active'
    END AS loan_status
FROM generate_series(1, 150) AS l
CROSS JOIN LATERAL (
    SELECT
        (ARRAY['home','auto','personal','business','education'])[((l * 3) % 5) + 1] AS loan_type,
        (ARRAY[7300, 1825, 1095, 1825, 2555])[((l * 3) % 5) + 1] AS term_days,
        (ARRAY[2000000, 500000, 200000, 1500000, 400000])[((l * 3) % 5) + 1]
            + ((l * 7919) % 100000) AS principal_amount
) lt;

-- ---------------------------------------------------------------------------
-- lending.loan_repayment — 6 instalments per loan (900 rows)
--   delinquent/written_off loans leave later instalments unpaid.
-- ---------------------------------------------------------------------------
INSERT INTO lending.loan_repayment (repayment_id, loan_id, customer_id, due_date,
                                    amount_due, amount_paid, paid_date)
SELECT
    ln.loan_id * 100 + idx AS repayment_id,
    ln.loan_id,
    ln.customer_id,
    ln.disbursal_date + idx * 30 AS due_date,
    emi.amount_due,
    CASE WHEN pays.is_paid THEN emi.amount_due ELSE 0 END AS amount_paid,
    CASE WHEN pays.is_paid THEN ln.disbursal_date + idx * 30 + ((ln.loan_id + idx) % 5)::int
         ELSE NULL END AS paid_date
FROM lending.loan ln
CROSS JOIN generate_series(1, 6) AS idx
CROSS JOIN LATERAL (
    SELECT ROUND((ln.principal_amount / 60.0)::numeric, 2) AS amount_due
) emi
CROSS JOIN LATERAL (
    SELECT CASE
        WHEN ln.loan_status = 'written_off' THEN idx <= 2
        WHEN ln.loan_status = 'delinquent'  THEN idx <= 4
        ELSE true
    END AS is_paid
) pays;

-- ---------------------------------------------------------------------------
-- lending.loan_delinquency — current delinquency status per loan (150 rows)
-- ---------------------------------------------------------------------------
INSERT INTO lending.loan_delinquency (loan_id, customer_id, as_of_date,
                                      dpd_bucket, overdue_amount)
SELECT
    ln.loan_id,
    ln.customer_id,
    CURRENT_DATE AS as_of_date,
    CASE ln.loan_status
        WHEN 'written_off' THEN '90+'
        WHEN 'delinquent'  THEN (ARRAY['1-30','31-60','61-90'])[((ln.loan_id * 3) % 3) + 1]
        ELSE 'current'
    END AS dpd_bucket,
    CASE WHEN ln.loan_status IN ('written_off', 'delinquent')
         THEN ROUND((ln.principal_amount / 60.0 * (1 + (ln.loan_id % 3)))::numeric, 2)
         ELSE 0
    END AS overdue_amount
FROM lending.loan ln;

-- ---------------------------------------------------------------------------
-- payments.beneficiary — 180 saved payees
-- ---------------------------------------------------------------------------
INSERT INTO payments.beneficiary (beneficiary_id, customer_id, beneficiary_name,
                                  account_number, ifsc)
SELECT
    b AS beneficiary_id,
    ((b * 17 + 9) % 200) + 1 AS customer_id,
    (ARRAY['Ravi Kumar','Sita Devi','Mohammed Ali','John Mathew','Anita Rao',
           'Deepak Nair','Farah Khan','Vijay Menon','Leela Krishnan','Sameer Shah'])
        [((b * 3) % 10) + 1] AS beneficiary_name,
    lpad(((b::bigint * 987654321) % 1000000000000)::text, 12, '0') AS account_number,
    (ARRAY['HDFC','ICIC','SBIN','AXIS','KKBK','PUNB'])[((b * 5) % 6) + 1]
        || '0' || lpad(((b * 7919) % 1000000)::text, 6, '0') AS ifsc
FROM generate_series(1, 180) AS b;

-- ---------------------------------------------------------------------------
-- payments.payment_instruction — 300 outbound payments (FK to beneficiary)
-- ---------------------------------------------------------------------------
INSERT INTO payments.payment_instruction (payment_id, customer_id, beneficiary_id,
                                          amount, currency, status, value_date)
SELECT
    pnum AS payment_id,
    ben.customer_id,
    ben.beneficiary_id,
    ROUND((((pnum * 5237 + 500) % 500000 + 500) / 1.0)::numeric, 2) AS amount,
    CASE WHEN pnum % 20 = 0 THEN 'USD' ELSE 'INR' END AS currency,
    (ARRAY['settled','settled','settled','submitted','returned','failed'])
        [((pnum * 3) % 6) + 1] AS status,
    CURRENT_DATE - ((pnum * 7919) % 200) AS value_date
FROM generate_series(1, 300) AS pnum
JOIN payments.beneficiary ben
  ON ben.beneficiary_id = ((pnum * 19 + 3) % 180) + 1;

-- ---------------------------------------------------------------------------
-- ops_noise.support_ticket — 60 backoffice tickets (never map-matches)
-- ---------------------------------------------------------------------------
INSERT INTO ops_noise.support_ticket (ticket_id, subject, priority, status, opened_at)
SELECT
    t AS ticket_id,
    (ARRAY['VPN access request','Laptop replacement','Printer offline',
           'Email quota exceeded','Password reset','Software install',
           'Meeting room booking','Badge not working'])[((t * 3) % 8) + 1]
        || ' #' || t AS subject,
    (ARRAY['low','medium','high','urgent'])[((t * 5) % 4) + 1] AS priority,
    (ARRAY['open','in_progress','resolved','closed'])[((t * 7) % 4) + 1] AS status,
    (CURRENT_DATE - ((t * 13) % 120))::timestamp + (t % 8) * INTERVAL '1 hour' AS opened_at
FROM generate_series(1, 60) AS t;

-- ---------------------------------------------------------------------------
-- ops_noise.facility_asset — 40 backoffice assets (never map-matches)
-- ---------------------------------------------------------------------------
INSERT INTO ops_noise.facility_asset (asset_id, asset_type, location, status)
SELECT
    a AS asset_id,
    (ARRAY['atm','server','printer','vehicle','ups','router'])[((a * 3) % 6) + 1] AS asset_type,
    (ARRAY['Mumbai HQ','Delhi Branch','Bengaluru DC','Chennai Branch',
           'Pune Office','Hyderabad DC'])[((a * 5) % 6) + 1] AS location,
    (ARRAY['in_service','maintenance','retired'])[((a * 7) % 3) + 1] AS status
FROM generate_series(1, 40) AS a;

-- ---------------------------------------------------------------------------
-- Integrity checks — raise EXCEPTION if anything looks wrong
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    cnt INTEGER;
BEGIN
    -- Volume floors (a failed INSERT in a multi-statement psql run does not
    -- abort the file — catch it here).
    SELECT COUNT(*) INTO cnt FROM crm.party;
    IF cnt <> 200 THEN RAISE EXCEPTION 'crm.party has % rows, expected 200', cnt; END IF;
    SELECT COUNT(*) INTO cnt FROM crm.party_contact;
    IF cnt <> 400 THEN RAISE EXCEPTION 'crm.party_contact has % rows, expected 400', cnt; END IF;
    SELECT COUNT(*) INTO cnt FROM crm.kyc_verification;
    IF cnt <> 200 THEN RAISE EXCEPTION 'crm.kyc_verification has % rows, expected 200', cnt; END IF;
    SELECT COUNT(*) INTO cnt FROM core_banking.account;
    IF cnt <> 300 THEN RAISE EXCEPTION 'core_banking.account has % rows, expected 300', cnt; END IF;
    SELECT COUNT(*) INTO cnt FROM core_banking.account_balance;
    IF cnt <> 300 THEN RAISE EXCEPTION 'core_banking.account_balance has % rows, expected 300', cnt; END IF;
    SELECT COUNT(*) INTO cnt FROM core_banking.deposit_transaction;
    IF cnt = 0 THEN RAISE EXCEPTION 'core_banking.deposit_transaction is empty'; END IF;
    SELECT COUNT(*) INTO cnt FROM cards.card;
    IF cnt <> 250 THEN RAISE EXCEPTION 'cards.card has % rows, expected 250', cnt; END IF;
    SELECT COUNT(*) INTO cnt FROM cards.card_limit;
    IF cnt <> 500 THEN RAISE EXCEPTION 'cards.card_limit has % rows, expected 500', cnt; END IF;
    SELECT COUNT(*) INTO cnt FROM cards.card_transaction;
    IF cnt = 0 THEN RAISE EXCEPTION 'cards.card_transaction is empty'; END IF;
    SELECT COUNT(*) INTO cnt FROM lending.loan;
    IF cnt <> 150 THEN RAISE EXCEPTION 'lending.loan has % rows, expected 150', cnt; END IF;
    SELECT COUNT(*) INTO cnt FROM lending.loan_repayment;
    IF cnt <> 900 THEN RAISE EXCEPTION 'lending.loan_repayment has % rows, expected 900', cnt; END IF;
    SELECT COUNT(*) INTO cnt FROM lending.loan_delinquency;
    IF cnt <> 150 THEN RAISE EXCEPTION 'lending.loan_delinquency has % rows, expected 150', cnt; END IF;
    SELECT COUNT(*) INTO cnt FROM payments.beneficiary;
    IF cnt <> 180 THEN RAISE EXCEPTION 'payments.beneficiary has % rows, expected 180', cnt; END IF;
    SELECT COUNT(*) INTO cnt FROM payments.payment_instruction;
    IF cnt <> 300 THEN RAISE EXCEPTION 'payments.payment_instruction has % rows, expected 300', cnt; END IF;

    -- Cross-system business-key resolution (no cross-schema FKs — data-level check):
    -- every customer_id in the product systems resolves to a crm.party.
    SELECT COUNT(*) INTO cnt FROM core_banking.account acc
    WHERE NOT EXISTS (SELECT 1 FROM crm.party p WHERE p.customer_id = acc.customer_id);
    IF cnt > 0 THEN RAISE EXCEPTION '% account rows with dangling customer_id', cnt; END IF;
    SELECT COUNT(*) INTO cnt FROM cards.card cd
    WHERE NOT EXISTS (SELECT 1 FROM crm.party p WHERE p.customer_id = cd.customer_id);
    IF cnt > 0 THEN RAISE EXCEPTION '% card rows with dangling customer_id', cnt; END IF;
    SELECT COUNT(*) INTO cnt FROM lending.loan ln
    WHERE NOT EXISTS (SELECT 1 FROM crm.party p WHERE p.customer_id = ln.customer_id);
    IF cnt > 0 THEN RAISE EXCEPTION '% loan rows with dangling customer_id', cnt; END IF;
    SELECT COUNT(*) INTO cnt FROM payments.beneficiary bn
    WHERE NOT EXISTS (SELECT 1 FROM crm.party p WHERE p.customer_id = bn.customer_id);
    IF cnt > 0 THEN RAISE EXCEPTION '% beneficiary rows with dangling customer_id', cnt; END IF;

    -- Balance semantics: available never exceeds ledger, never negative.
    SELECT COUNT(*) INTO cnt FROM core_banking.account_balance
    WHERE available_balance > ledger_balance OR available_balance < 0;
    IF cnt > 0 THEN RAISE EXCEPTION '% balance rows with available>ledger or negative', cnt; END IF;

    -- Card semantics: debit cards carry no credit limit; others do.
    SELECT COUNT(*) INTO cnt FROM cards.card
    WHERE (card_type = 'debit') <> (credit_limit = 0);
    IF cnt > 0 THEN RAISE EXCEPTION '% card rows with inconsistent debit/credit_limit', cnt; END IF;

    -- Repayment semantics: amount_paid present iff paid_date present.
    SELECT COUNT(*) INTO cnt FROM lending.loan_repayment
    WHERE (amount_paid > 0) <> (paid_date IS NOT NULL);
    IF cnt > 0 THEN RAISE EXCEPTION '% repayment rows with inconsistent paid fields', cnt; END IF;

    -- Delinquency semantics: overdue amount is positive iff not current.
    SELECT COUNT(*) INTO cnt FROM lending.loan_delinquency
    WHERE (dpd_bucket = 'current') <> (overdue_amount = 0);
    IF cnt > 0 THEN RAISE EXCEPTION '% delinquency rows with inconsistent overdue_amount', cnt; END IF;

    RAISE NOTICE 'Integrity checks passed.';
END $$;

-- ---------------------------------------------------------------------------
-- Refresh planner statistics so a freshly-loaded estate reports real
-- volumetrics (pg_class.reltuples reads 0/-1 until ANALYZE runs).
-- ---------------------------------------------------------------------------
ANALYZE;

-- ---------------------------------------------------------------------------
-- Volume summary
-- ---------------------------------------------------------------------------
SELECT 'crm.party'                        AS table_name, COUNT(*) AS row_count FROM crm.party
UNION ALL SELECT 'crm.party_contact',                COUNT(*) FROM crm.party_contact
UNION ALL SELECT 'crm.kyc_verification',             COUNT(*) FROM crm.kyc_verification
UNION ALL SELECT 'core_banking.account',             COUNT(*) FROM core_banking.account
UNION ALL SELECT 'core_banking.account_balance',     COUNT(*) FROM core_banking.account_balance
UNION ALL SELECT 'core_banking.deposit_transaction', COUNT(*) FROM core_banking.deposit_transaction
UNION ALL SELECT 'cards.card',                       COUNT(*) FROM cards.card
UNION ALL SELECT 'cards.card_limit',                 COUNT(*) FROM cards.card_limit
UNION ALL SELECT 'cards.card_transaction',           COUNT(*) FROM cards.card_transaction
UNION ALL SELECT 'lending.loan',                     COUNT(*) FROM lending.loan
UNION ALL SELECT 'lending.loan_repayment',           COUNT(*) FROM lending.loan_repayment
UNION ALL SELECT 'lending.loan_delinquency',         COUNT(*) FROM lending.loan_delinquency
UNION ALL SELECT 'payments.beneficiary',             COUNT(*) FROM payments.beneficiary
UNION ALL SELECT 'payments.payment_instruction',     COUNT(*) FROM payments.payment_instruction
UNION ALL SELECT 'ops_noise.support_ticket',         COUNT(*) FROM ops_noise.support_ticket
UNION ALL SELECT 'ops_noise.facility_asset',         COUNT(*) FROM ops_noise.facility_asset
ORDER BY table_name;
