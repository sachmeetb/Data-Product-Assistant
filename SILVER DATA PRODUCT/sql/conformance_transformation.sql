-- ==============================================================================
-- BIGQUERY STANDARD SQL: MULTI-SOURCE CONFORMANCE TRANSFORMATION
-- Sources: Hogan Deposit, AFS Commercial, TSYS Credit Card, ACLS Consumer Loan
-- Target:  silver_customer & silver_customer_source_xref
-- Methodology: Tony Giordano "Conforming Logic" (Steps 4 & 5)
-- ==============================================================================

WITH 
-- ── STEP 4.1: STANDARDIZE HOGAN DEPOSIT / CIS ─────────────────────────────────
std_hogan AS (
    SELECT
        'HOGAN' AS source_system,
        'brz_hogan_ci_customer' AS source_entity,
        TRIM(CI_CUST_NUM) AS source_customer_id,
        COALESCE(cw_type.cdm_code, 'INDIVIDUAL') AS customer_type,
        INITCAP(SPLIT(CI_LEGAL_NAME, ',')[SAFE_OFFSET(1)]) || ' ' || INITCAP(SPLIT(CI_LEGAL_NAME, ',')[SAFE_OFFSET(0)]) AS legal_name,
        INITCAP(SPLIT(TRIM(SPLIT(CI_LEGAL_NAME, ',')[SAFE_OFFSET(1)]), ' ')[SAFE_OFFSET(0)]) AS first_name,
        INITCAP(TRIM(SPLIT(CI_LEGAL_NAME, ',')[SAFE_OFFSET(0)])) AS last_name,
        REGEXP_REPLACE(CAST(CI_TAX_ID AS STRING), r'\D', '') AS tax_id_clean,
        PARSE_DATE('%Y%m%d', CAST(CI_BIRTH_DT AS STRING)) AS date_of_birth,
        CONCAT('+1', REGEXP_REPLACE(CAST(CI_PHONE AS STRING), r'\D', '')) AS phone_e164,
        CAST(NULL AS STRING) AS email,
        SUBSTR(TRIM(CI_POST_CODE), 1, 5) AS postal_code,
        COALESCE(cw_stat.cdm_code, 'ACTIVE') AS relationship_status,
        1 AS sor_rank,  -- Hogan is Rank 1 System of Record for KYC Name/DOB
        TIMESTAMP(CI_EXTRACT_TS) AS extract_ts
    FROM `{project_id}.{bronze_dataset}.brz_hogan_ci_customer` h
    LEFT JOIN `{project_id}.{silver_dataset}.ref_crosswalk_customer_type` cw_type
        ON cw_type.source_system = 'HOGAN' AND cw_type.source_code = h.CI_CUST_TYPE
    LEFT JOIN `{project_id}.{silver_dataset}.ref_crosswalk_status` cw_stat
        ON cw_stat.source_system = 'HOGAN' AND cw_stat.source_code = h.CI_STATUS_CD
),

-- ── STEP 4.2: STANDARDIZE AFS COMMERCIAL LENDING ──────────────────────────────
std_afs AS (
    SELECT
        'AFS' AS source_system,
        'brz_afs_borrower' AS source_entity,
        TRIM(BORROWER_ID) AS source_customer_id,
        COALESCE(cw_type.cdm_code, 'ORGANIZATION') AS customer_type,
        INITCAP(TRIM(LEGAL_ENTITY_NAME)) AS legal_name,
        INITCAP(SPLIT(TRIM(PRIMARY_CONTACT_NAME), ' ')[SAFE_OFFSET(0)]) AS first_name,
        INITCAP(SPLIT(TRIM(PRIMARY_CONTACT_NAME), ' ')[SAFE_OFFSET(1)]) AS last_name,
        REGEXP_REPLACE(CAST(TAX_EIN AS STRING), r'\D', '') AS tax_id_clean,
        CAST(NULL AS DATE) AS date_of_birth,
        CONCAT('+1', REGEXP_REPLACE(CAST(CONTACT_PHONE AS STRING), r'\D', '')) AS phone_e164,
        LOWER(TRIM(CONTACT_EMAIL)) AS email,
        REGEXP_EXTRACT(BUSINESS_ADDRESS, r'\b\d{5}\b') AS postal_code,
        COALESCE(cw_stat.cdm_code, 'ACTIVE') AS relationship_status,
        2 AS sor_rank,
        TIMESTAMP(LAST_REVIEW_DATE) AS extract_ts
    FROM `{project_id}.{bronze_dataset}.brz_afs_borrower` a
    LEFT JOIN `{project_id}.{silver_dataset}.ref_crosswalk_customer_type` cw_type
        ON cw_type.source_system = 'AFS' AND cw_type.source_code = a.BORROWER_CLASS
    LEFT JOIN `{project_id}.{silver_dataset}.ref_crosswalk_status` cw_stat
        ON cw_stat.source_system = 'AFS' AND cw_stat.source_code = a.STATUS
),

-- ── STEP 4.3: STANDARDIZE TSYS / FDR CREDIT CARDS ─────────────────────────────
std_tsys AS (
    SELECT
        'TSYS' AS source_system,
        'brz_tsys_cardholder' AS source_entity,
        TRIM(cardholder_id) AS source_customer_id,
        COALESCE(cw_type.cdm_code, 'INDIVIDUAL') AS customer_type,
        INITCAP(TRIM(embossed_name)) AS legal_name,
        INITCAP(SPLIT(TRIM(embossed_name), ' ')[SAFE_OFFSET(0)]) AS first_name,
        INITCAP(SPLIT(TRIM(embossed_name), ' ')[SAFE_OFFSET(2)]) AS last_name,
        REGEXP_REPLACE(CAST(tax_ssn AS STRING), r'\D', '') AS tax_id_clean,
        CAST(NULL AS DATE) AS date_of_birth,
        CONCAT('+1', SUBSTR(REGEXP_REPLACE(CAST(mobile_phone AS STRING), r'\D', ''), -10)) AS phone_e164,
        CAST(NULL AS STRING) AS email,
        SUBSTR(TRIM(billing_postal_code), 1, 5) AS postal_code,
        COALESCE(cw_stat.cdm_code, 'ACTIVE') AS relationship_status,
        4 AS sor_rank,
        TIMESTAMP(last_active_date) AS extract_ts
    FROM `{project_id}.{bronze_dataset}.brz_tsys_cardholder` t
    LEFT JOIN `{project_id}.{silver_dataset}.ref_crosswalk_customer_type` cw_type
        ON cw_type.source_system = 'TSYS' AND cw_type.source_code = t.card_type
    LEFT JOIN `{project_id}.{silver_dataset}.ref_crosswalk_status` cw_stat
        ON cw_stat.source_system = 'TSYS' AND cw_stat.source_code = t.card_status
),

-- ── STEP 4.4: STANDARDIZE ACLS CONSUMER LOANS ─────────────────────────────────
std_acls AS (
    SELECT
        'ACLS' AS source_system,
        'brz_acls_applicant' AS source_entity,
        TRIM(APPLICANT_ID) AS source_customer_id,
        'INDIVIDUAL' AS customer_type,
        TRIM(CONCAT(COALESCE(FIRST_NAME, ''), ' ', COALESCE(MIDDLE_NAME, ''), ' ', COALESCE(LAST_NAME, ''))) AS legal_name,
        INITCAP(TRIM(FIRST_NAME)) AS first_name,
        INITCAP(TRIM(LAST_NAME)) AS last_name,
        REGEXP_REPLACE(CAST(NATIONAL_ID AS STRING), r'\D', '') AS tax_id_clean,
        PARSE_DATE('%m/%d/%Y', CAST(DOB AS STRING)) AS date_of_birth,
        CONCAT('+1', SUBSTR(REGEXP_REPLACE(CAST(CELL_PHONE AS STRING), r'\D', ''), -10)) AS phone_e164,
        LOWER(TRIM(EMAIL)) AS email,
        SUBSTR(TRIM(POSTAL_CODE), 1, 5) AS postal_code,
        COALESCE(cw_stat.cdm_code, 'ACTIVE') AS relationship_status,
        3 AS sor_rank,
        TIMESTAMP(APP_DATE) AS extract_ts
    FROM `{project_id}.{bronze_dataset}.brz_acls_applicant` c
    LEFT JOIN `{project_id}.{silver_dataset}.ref_crosswalk_status` cw_stat
        ON cw_stat.source_system = 'ACLS' AND cw_stat.source_code = c.APP_STATUS
),

-- ── UNION ALL STANDARDIZED BRONZE INPUTS ──────────────────────────────────────
all_standardized AS (
    SELECT * FROM std_hogan
    UNION ALL
    SELECT * FROM std_afs
    UNION ALL
    SELECT * FROM std_tsys
    UNION ALL
    SELECT * FROM std_acls
),

-- ── STEP 5.1: IDENTITY RESOLUTION (DETERMINISTIC & COMPOSITE KEYING) ─────────
resolved_identities AS (
    SELECT
        *,
        -- Assign canonical cluster seed based on Tier 1 (Tax ID) or Tier 2 (Phone + DOB)
        COALESCE(
            NULLIF(tax_id_clean, ''),
            CONCAT(CAST(date_of_birth AS STRING), '_', phone_e164),
            CONCAT(LOWER(last_name), '_', phone_e164),
            CONCAT(source_system, '_', source_customer_id)
        ) AS cluster_seed
    FROM all_standardized
),

assigned_customer_keys AS (
    SELECT
        *,
        CONCAT('CUST-', SUBSTR(TO_HEX(SHA256(cluster_seed)), 1, 12)) AS customer_key
    FROM resolved_identities
),

-- ── STEP 5.2: SURVIVORSHIP & GOLDEN ATTRIBUTE SELECTION ────────────────────────
golden_ranked AS (
    SELECT
        *,
        ROW_NUMBER() OVER(PARTITION BY customer_key ORDER BY sor_rank ASC, extract_ts DESC) AS legal_name_rank,
        ROW_NUMBER() OVER(PARTITION BY customer_key ORDER BY extract_ts DESC) AS phone_recency_rank,
        ROW_NUMBER() OVER(PARTITION BY customer_key ORDER BY (email IS NOT NULL) DESC, extract_ts DESC) AS email_rank,
        ROW_NUMBER() OVER(PARTITION BY customer_key ORDER BY (date_of_birth IS NOT NULL) DESC, sor_rank ASC) AS dob_rank
    FROM assigned_customer_keys
)

-- ── FINAL OUTPUT: SILVER CUSTOMER GOLDEN RECORDS ──────────────────────────────
SELECT
    k.customer_key,
    MAX(CASE WHEN k.customer_type = 'ORGANIZATION' THEN 'ORGANIZATION' ELSE 'INDIVIDUAL' END) AS customer_type,
    MAX(CASE WHEN r.legal_name_rank = 1 THEN r.legal_name END) AS legal_name,
    MAX(CASE WHEN r.legal_name_rank = 1 THEN r.first_name END) AS first_name,
    MAX(CASE WHEN r.legal_name_rank = 1 THEN r.last_name END) AS last_name,
    MAX(CASE WHEN r.dob_rank = 1 THEN r.date_of_birth END) AS date_of_birth,
    MAX(CASE WHEN NULLIF(r.tax_id_clean, '') IS NOT NULL THEN r.tax_id_clean END) AS tax_id_normalized,
    CONCAT('***-**-', SUBSTR(MAX(CASE WHEN NULLIF(r.tax_id_clean, '') IS NOT NULL THEN r.tax_id_clean END), -4)) AS tax_id_masked,
    MAX(CASE WHEN r.phone_recency_rank = 1 THEN r.phone_e164 END) AS primary_phone_e164,
    MAX(CASE WHEN r.email_rank = 1 THEN r.email END) AS primary_email,
    MAX(CASE WHEN r.legal_name_rank = 1 THEN r.postal_code END) AS primary_postal_code,
    MAX(CASE WHEN r.relationship_status = 'ACTIVE' THEN 'ACTIVE' ELSE r.relationship_status END) AS relationship_status,
    MAX(CASE WHEN r.legal_name_rank = 1 THEN r.source_system END) AS survivorship_source,
    ARRAY_AGG(DISTINCT r.source_system) AS contributing_sources,
    COUNT(1) AS source_record_count,
    CURRENT_TIMESTAMP() AS effective_from_ts,
    CAST(NULL AS TIMESTAMP) AS effective_to_ts,
    TRUE AS is_current,
    'VALID' AS dq_status
FROM assigned_customer_keys k
JOIN golden_ranked r ON k.customer_key = r.customer_key AND k.source_customer_id = r.source_customer_id
GROUP BY k.customer_key;
