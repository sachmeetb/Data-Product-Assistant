-- =============================================================================
-- Data Workbench sample dataset — Banking / Customer domain
-- File 2 of 3: seed data
--
-- Strategy: everything generated deterministically (id-arithmetic +
-- generate_series, NO random()) so reloads are reproducible. One deliberate
-- deviation from byte-identical reloads: analytics.transaction_ledger
-- timestamps are anchored to CURRENT_DATE so the churn-risk recency signal
-- (days_since_last_txn over the last 90 days) stays meaningful whenever the
-- demo is run. Reloads are identical within a day, and cohort membership is
-- identical always.
--
-- Churn cohorts (by customer_id — drives churn_risk_score demos):
--   churned   customer_id % 7 = 3            → no ledger activity in ≥91 days
--   declining customer_id % 11 = 5 (rest)    → sparse recent activity
--   active    everyone else                  → regular activity in last 90 days
--
-- churn_risk_score reference implementation (per customer, last 90 days):
--   days_since_last_txn = CURRENT_DATE - MAX(txn_timestamp)::date
--   txn_count_90d       = COUNT(txn_id) within the window
--   score = GREATEST(0, LEAST(100, days_since_last_txn*2 - txn_count_90d*5))
--   0 = active, 100 = almost certainly churned
-- =============================================================================

SET search_path TO edw, public;

-- ---------------------------------------------------------------------------
-- edw.customer — 500 rows, generated from Indian name/address pools
--   customer_type: hni when n % 50 = 0 (10), corporate when n % 12 = 0 (40),
--                  else retail (450)
--   customer_segment: business (corporate), private (hni); retail split
--                  mass ~60% / affluent ~30% / premium ~10%
--   kyc_status:    expired when n % 20 = 7 (25), pending when n % 10 = 3 (50),
--                  else verified (425)
-- ---------------------------------------------------------------------------
INSERT INTO edw.customer (customer_id, name, gender, pan_and_aadhaar_number,
                          customer_type, email, mobile_number, address,
                          kyc_status, onboarded_date, customer_segment)
SELECT
    n AS customer_id,
    nm.full_name,
    g.gender,
    chr(65 + (n * 7)  % 26) ||
    chr(65 + (n * 11) % 26) ||
    chr(65 + (n * 13) % 26) ||
    CASE WHEN t.ctype = 'corporate' THEN 'C' ELSE 'P' END ||
    left(nm.lname, 1) ||
    lpad(((n * 7919) % 10000)::text, 4, '0') ||
    chr(65 + (n * 23) % 26)                            AS pan_and_aadhaar_number,
    t.ctype AS customer_type,
    CASE WHEN t.ctype = 'corporate'
         THEN 'finance' || n || '@' || lower(nm.lname) || '.example.in'
         ELSE lower(nm.fname) || '.' || lower(nm.lname) || n || '@example.in'
    END AS email,
    '+91-9' || lpad(((n * 686593) % 1000000000)::text, 9, '0') AS mobile_number,
    ((n % 240) + 1)::text || ' ' ||
    (ARRAY['MG Road','Nehru Street','Gandhi Nagar Main Road','Link Road',
           'Station Road','Mount Road','Brigade Road','Park Street',
           'FC Road','Anna Salai','Ring Road','Rajpath Lane'])[((n * 3) % 12) + 1] || ', ' ||
    (ARRAY['Andheri West','Koramangala','Banjara Hills','T Nagar','Salt Lake',
           'Aundh','Dwarka','Indiranagar','Vashi','Alwarpet'])[((n * 5) % 10) + 1] || ', ' ||
    (ARRAY['Mumbai 400053','New Delhi 110001','Bengaluru 560034','Chennai 600017',
           'Hyderabad 500034','Pune 411007','Kolkata 700091','Ahmedabad 380015'])[((n * 7) % 8) + 1]
        AS address,
    CASE WHEN n % 20 = 7 THEN 'expired'
         WHEN n % 10 = 3 THEN 'pending'
         ELSE 'verified'
    END AS kyc_status,
    DATE '2015-01-06' + ((n * 7919) % 3800) AS onboarded_date,
    CASE t.ctype
        WHEN 'corporate' THEN 'business'
        WHEN 'hni'       THEN 'private'
        ELSE CASE WHEN n % 10 <= 5 THEN 'mass'
                  WHEN n % 10 <= 8 THEN 'affluent'
                  ELSE 'premium'
             END
    END AS customer_segment
FROM generate_series(1, 500) AS n
CROSS JOIN LATERAL (
    SELECT CASE WHEN n % 50 = 0 THEN 'hni'
                WHEN n % 12 = 0 THEN 'corporate'
                ELSE 'retail'
           END AS ctype
) t
CROSS JOIN LATERAL (
    SELECT CASE WHEN t.ctype = 'corporate' THEN 'O'
                WHEN n % 2 = 0 THEN 'F'
                ELSE 'M'
           END AS gender
) g
CROSS JOIN LATERAL (
    SELECT
        (ARRAY['Sharma','Verma','Gupta','Mehta','Patel','Reddy','Nair','Iyer',
               'Menon','Kulkarni','Deshpande','Joshi','Chopra','Malhotra','Bhat',
               'Rao','Das','Mukherjee','Banerjee','Chatterjee','Singh','Kaur',
               'Agarwal','Mishra','Pillai'])[((n * 7) % 25) + 1] AS lname,
        CASE WHEN g.gender = 'M'
             THEN (ARRAY['Aarav','Vihaan','Aditya','Arjun','Rohan','Karan','Rajesh',
                         'Suresh','Amit','Vikram','Sanjay','Manoj','Deepak','Nikhil',
                         'Rahul','Anil','Prakash','Harish','Sunil','Mohan'])[((n * 3) % 20) + 1]
             ELSE (ARRAY['Priya','Ananya','Divya','Kavya','Sneha','Pooja','Neha',
                         'Anjali','Shreya','Ritu','Sunita','Meena','Lakshmi','Aishwarya',
                         'Nandini','Swati','Rekha','Pallavi','Geeta','Isha'])[((n * 5) % 20) + 1]
        END AS fname
) nm0
CROSS JOIN LATERAL (
    SELECT nm0.lname, nm0.fname,
           CASE WHEN t.ctype = 'corporate'
                THEN nm0.lname || ' ' ||
                     (ARRAY['Textiles Pvt Ltd','Traders Co','Industries Ltd',
                            'Exports Pvt Ltd','Agro Foods Ltd','Infotech Pvt Ltd',
                            'Logistics Ltd','Motors Pvt Ltd'])[((n * 11) % 8) + 1]
                ELSE nm0.fname || ' ' || nm0.lname
           END AS full_name
) nm;

-- ---------------------------------------------------------------------------
-- retail.customer_profile — 500 rows, FULLY CONSISTENT with edw.customer on
-- the shared columns (generated FROM it), plus lake-only consent/preference
-- attributes.
-- ---------------------------------------------------------------------------
INSERT INTO retail.customer_profile (customer_id, name, gender, customer_type,
                                     citizenship_country, onboarding_channel,
                                     email, mobile_number,
                                     communication_channel_pref,
                                     marketing_opt_in_flag, kyc_status,
                                     kyc_verification_date, onboarded_date)
SELECT
    c.customer_id,
    c.name,
    c.gender,
    c.customer_type,
    CASE WHEN c.customer_type = 'hni' AND c.customer_id % 3 = 0 THEN 'SG'
         WHEN c.customer_type = 'hni' AND c.customer_id % 3 = 1 THEN 'AE'
         ELSE 'IN'
    END AS citizenship_country,
    (ARRAY['branch','online','mobile_app','relationship_manager'])
        [CASE WHEN c.customer_type <> 'retail' THEN 4
              ELSE ((c.customer_id * 3) % 3) + 1 END] AS onboarding_channel,
    c.email,
    c.mobile_number,
    (ARRAY['email','sms','whatsapp','phone'])[((c.customer_id * 3) % 4) + 1]
        AS communication_channel_pref,
    (c.customer_id % 10) NOT IN (0, 7, 8) AS marketing_opt_in_flag,
    c.kyc_status,
    CASE c.kyc_status
        WHEN 'verified' THEN c.onboarded_date + ((c.customer_id * 7) % 30)::int
        WHEN 'expired'  THEN c.onboarded_date + 15
        ELSE NULL
    END AS kyc_verification_date,
    c.onboarded_date
FROM edw.customer c;

-- ---------------------------------------------------------------------------
-- edw.campaign_response — 12 campaigns (101..112) spread Apr 2025 → Jun 2026;
-- each targets a deterministic ~40% of customers.
--   conversion_status: r = (cid*11 + camp*17) % 100
--     r < 8 → converted, r < 33 → engaged, r < 95 → no_response,
--     else unsubscribed
--   conversion_value only when converted; converted always opened.
-- ---------------------------------------------------------------------------
INSERT INTO edw.campaign_response (customer_id, campaign_id, conversion_status,
                                   conversion_value, message_sent_date,
                                   channel, opened_flag)
SELECT
    c.customer_id,
    camp AS campaign_id,
    st.status AS conversion_status,
    CASE WHEN st.status = 'converted'
         THEN ROUND((((c.customer_id * 7919 + camp * 104729) % 450000 + 50000) / 100.0)::numeric, 2)
    END AS conversion_value,
    DATE '2025-04-07' + (camp - 101) * 38 + (c.customer_id % 5)::int AS message_sent_date,
    (ARRAY['email','sms','whatsapp','push'])[((c.customer_id + camp) % 4) + 1] AS channel,
    (((c.customer_id * 7 + camp * 13) % 100) < 55 OR st.status = 'converted') AS opened_flag
FROM generate_series(101, 112) AS camp
JOIN edw.customer c
  ON (c.customer_id * camp) % 5 < 2          -- deterministic ~40% audience
CROSS JOIN LATERAL (
    SELECT CASE WHEN r < 8  THEN 'converted'
                WHEN r < 33 THEN 'engaged'
                WHEN r < 95 THEN 'no_response'
                ELSE 'unsubscribed'
           END AS status
    FROM (SELECT (c.customer_id * 11 + camp * 17) % 100 AS r) x
) st;

-- ---------------------------------------------------------------------------
-- analytics.transaction_ledger — ~15,600 rows over 800 synthetic accounts.
--   Owner: ((account_id * 13 + 7) % 500) + 1 (customer business key).
--   Timestamps anchored to CURRENT_DATE (see header) with per-cohort recency:
--     churned   → every txn 91..365 days ago
--     declining → 1-in-6 txns 20..89 days ago, rest 91..365
--     active    → 2-in-3 txns 0..89 days ago, rest 91..365
-- ---------------------------------------------------------------------------
INSERT INTO analytics.transaction_ledger (txn_id, account_id, customer_id,
                                          amount, currency, txn_timestamp,
                                          channel, status, merchant_id, mcc,
                                          debit_credit, gl_account, source_system)
SELECT
    (a::bigint * 100) + g.idx AS txn_id,
    a AS account_id,
    own.customer_id,
    ROUND(((((a * 31 + g.idx * 7919) % 999000) + 1000)
           * CASE WHEN own.customer_type = 'corporate' THEN 25 ELSE 1 END
          / 100.0)::numeric, 2) AS amount,
    CASE WHEN own.customer_type = 'corporate' AND a % 5 = 0 THEN 'USD'
         WHEN own.customer_type = 'corporate' AND a % 5 = 1 THEN 'EUR'
         ELSE 'INR'
    END AS currency,
    (CURRENT_DATE - co.offset_days)::timestamp
        + (g.idx % 24) * INTERVAL '1 hour'
        + ((a + g.idx) % 60) * INTERVAL '1 minute' AS txn_timestamp,
    ch.channel,
    CASE WHEN (a + g.idx * 7) % 100 < 96 THEN 'posted'
         WHEN (a + g.idx * 7) % 100 < 99 THEN 'pending'
         ELSE 'reversed'
    END AS status,
    CASE WHEN ch.channel IN ('pos', 'online', 'upi')
         THEN 'MER' || lpad(ch.mnum::text, 6, '0')
    END AS merchant_id,
    CASE WHEN ch.channel IN ('pos', 'online', 'upi')
         THEN (ARRAY['5411','5812','5541','4900','5912','5651','4121','5732',
                     '8062','8220','4814','5311'])[(ch.mnum % 12) + 1]
    END AS mcc,
    CASE WHEN (a * 3 + g.idx) % 10 < 7 THEN 'D' ELSE 'C' END AS debit_credit,
    CASE ch.channel
        WHEN 'atm'    THEN 'GL1101'
        WHEN 'branch' THEN 'GL1102'
        WHEN 'pos'    THEN 'GL4501'
        WHEN 'online' THEN 'GL4502'
        ELSE               'GL4503'
    END AS gl_account,
    CASE WHEN ch.channel IN ('pos', 'online') THEN 'CARDS'
         WHEN ch.channel = 'upi'              THEN 'UPI-GW'
         ELSE                                      'CBS'
    END AS source_system
FROM generate_series(1, 800) AS a
JOIN edw.customer own ON own.customer_id = ((a * 13 + 7) % 500) + 1
CROSS JOIN LATERAL generate_series(1, 10 + ((a * 7) % 20)) AS g(idx)
CROSS JOIN LATERAL (
    SELECT CASE
        -- churned cohort: nothing in the last 90 days
        WHEN own.customer_id % 7 = 3
            THEN 91 + ((a * 7919 + g.idx * 104729) % 275)
        -- declining cohort: sparse recent, mostly old
        WHEN own.customer_id % 11 = 5
            THEN CASE WHEN g.idx % 6 = 0
                      THEN 20 + ((a + g.idx * 37) % 70)
                      ELSE 91 + ((a * 7919 + g.idx * 104729) % 275)
                 END
        -- active cohort: regular recent activity
        ELSE CASE WHEN g.idx % 3 = 0
                  THEN 91 + ((a * 7919 + g.idx * 104729) % 275)
                  ELSE (a * 7919 + g.idx * 104729) % 90
             END
    END AS offset_days
) co
CROSS JOIN LATERAL (
    SELECT (ARRAY['upi','upi','upi','upi','upi','upi','upi','upi',
                  'pos','pos','pos','pos',
                  'online','online','online',
                  'atm','atm','atm',
                  'branch','branch'])[((a * 3 + g.idx * 7) % 20) + 1] AS channel,
           ((a + g.idx * 7) % 400) + 1 AS mnum
) ch;

-- ---------------------------------------------------------------------------
-- marketing.campaign_audience — the marketing team's published mart, derived
-- from campaign_response + the customer master's segment (lineage the
-- Workbench should recover during discovery/mapping).
-- ---------------------------------------------------------------------------
INSERT INTO marketing.campaign_audience (customer_id, customer_segment,
                                         campaign_id, conversion_status,
                                         conversion_value, message_sent_date,
                                         channel, opened_flag)
SELECT r.customer_id,
       c.customer_segment,
       r.campaign_id,
       r.conversion_status,
       r.conversion_value,
       r.message_sent_date,
       r.channel,
       r.opened_flag
FROM edw.campaign_response r
JOIN edw.customer c USING (customer_id);

-- ---------------------------------------------------------------------------
-- Integrity checks — raise EXCEPTION if anything looks wrong
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    cnt INTEGER;
BEGIN
    -- Volume floor: every table must actually have rows (a failed INSERT in a
    -- multi-statement psql run does not abort the file — catch it here).
    SELECT COUNT(*) INTO cnt FROM edw.customer;
    IF cnt <> 500 THEN RAISE EXCEPTION 'edw.customer has % rows, expected 500', cnt; END IF;
    SELECT COUNT(*) INTO cnt FROM retail.customer_profile;
    IF cnt <> 500 THEN RAISE EXCEPTION 'retail.customer_profile has % rows, expected 500', cnt; END IF;
    SELECT COUNT(*) INTO cnt FROM edw.campaign_response;
    IF cnt = 0 THEN RAISE EXCEPTION 'edw.campaign_response is empty'; END IF;
    SELECT COUNT(*) INTO cnt FROM analytics.transaction_ledger;
    IF cnt = 0 THEN RAISE EXCEPTION 'analytics.transaction_ledger is empty'; END IF;

    -- Mart coherence: campaign_audience is exactly campaign_response × the
    -- master's segment.
    SELECT COUNT(*) INTO cnt FROM (
        SELECT customer_id, campaign_id FROM edw.campaign_response
        EXCEPT SELECT customer_id, campaign_id FROM marketing.campaign_audience
    ) missing;
    IF cnt > 0 THEN RAISE EXCEPTION 'campaign_audience is missing % campaign_response rows', cnt; END IF;
    SELECT COUNT(*) INTO cnt
    FROM marketing.campaign_audience a
    JOIN edw.customer c USING (customer_id)
    WHERE a.customer_segment <> c.customer_segment;
    IF cnt > 0 THEN RAISE EXCEPTION '% campaign_audience rows with stale customer_segment', cnt; END IF;

    -- Cross-system consistency: retail profile mirrors the EDW master exactly
    -- on the shared columns (fully-consistent-by-design).
    SELECT COUNT(*) INTO cnt
    FROM edw.customer c
    JOIN retail.customer_profile p USING (customer_id)
    WHERE c.name <> p.name OR c.gender <> p.gender
       OR c.customer_type <> p.customer_type OR c.email <> p.email
       OR c.mobile_number <> p.mobile_number OR c.kyc_status <> p.kyc_status
       OR c.onboarded_date <> p.onboarded_date;
    IF cnt > 0 THEN RAISE EXCEPTION '% profile rows drift from the EDW master', cnt; END IF;

    SELECT COUNT(*) INTO cnt FROM retail.customer_profile p
    WHERE NOT EXISTS (SELECT 1 FROM edw.customer c WHERE c.customer_id = p.customer_id);
    IF cnt > 0 THEN RAISE EXCEPTION '% profile rows have no EDW master row', cnt; END IF;

    -- Ledger customer ids resolve against the master (no FK across schemas —
    -- data-level check).
    SELECT COUNT(*) INTO cnt FROM analytics.transaction_ledger t
    WHERE NOT EXISTS (SELECT 1 FROM edw.customer c WHERE c.customer_id = t.customer_id);
    IF cnt > 0 THEN RAISE EXCEPTION '% ledger rows with dangling customer_id', cnt; END IF;

    -- Churned cohort really is silent for 90 days.
    SELECT COUNT(*) INTO cnt FROM analytics.transaction_ledger
    WHERE customer_id % 7 = 3
      AND txn_timestamp >= CURRENT_DATE - 90;
    IF cnt > 0 THEN RAISE EXCEPTION '% recent transactions found for churned-cohort customers', cnt; END IF;

    -- conversion_value present iff converted; converted implies opened.
    SELECT COUNT(*) INTO cnt FROM edw.campaign_response
    WHERE (conversion_status = 'converted') <> (conversion_value IS NOT NULL)
       OR (conversion_status = 'converted' AND NOT opened_flag);
    IF cnt > 0 THEN RAISE EXCEPTION '% campaign rows with inconsistent conversion fields', cnt; END IF;

    -- merchant_id / mcc null-pairing and channel consistency.
    SELECT COUNT(*) INTO cnt FROM analytics.transaction_ledger
    WHERE (merchant_id IS NULL) <> (mcc IS NULL)
       OR (channel IN ('atm', 'branch') AND merchant_id IS NOT NULL)
       OR (channel IN ('pos', 'online', 'upi') AND merchant_id IS NULL);
    IF cnt > 0 THEN RAISE EXCEPTION '% ledger rows with inconsistent merchant_id/mcc/channel', cnt; END IF;

    -- KYC verification date semantics.
    SELECT COUNT(*) INTO cnt FROM retail.customer_profile
    WHERE (kyc_status = 'pending') <> (kyc_verification_date IS NULL);
    IF cnt > 0 THEN RAISE EXCEPTION '% profile rows with inconsistent kyc_verification_date', cnt; END IF;

    RAISE NOTICE 'Integrity checks passed.';
END $$;

-- ---------------------------------------------------------------------------
-- Volume summary + churn cohort preview
-- ---------------------------------------------------------------------------
SELECT 'edw.customer' AS table_name, COUNT(*) AS row_count FROM edw.customer
UNION ALL SELECT 'edw.campaign_response',        COUNT(*) FROM edw.campaign_response
UNION ALL SELECT 'retail.customer_profile',      COUNT(*) FROM retail.customer_profile
UNION ALL SELECT 'analytics.transaction_ledger', COUNT(*) FROM analytics.transaction_ledger
UNION ALL SELECT 'marketing.campaign_audience',  COUNT(*) FROM marketing.campaign_audience
ORDER BY table_name;

-- churn_risk_score distribution preview (reference implementation)
SELECT CASE WHEN score = 0 THEN '0 (active)'
            WHEN score < 40 THEN '1-39 (watch)'
            WHEN score < 100 THEN '40-99 (at risk)'
            ELSE '100 (churned)' END AS churn_band,
       COUNT(*) AS customers
FROM (
    SELECT customer_id,
           GREATEST(0, LEAST(100,
               (CURRENT_DATE - MAX(txn_timestamp)::date) * 2
             - COUNT(txn_id) FILTER (WHERE txn_timestamp >= CURRENT_DATE - 90) * 5
           )) AS score
    FROM analytics.transaction_ledger
    GROUP BY customer_id
) s
GROUP BY 1 ORDER BY MIN(score);
