-- ==============================================================================
-- BIGQUERY STANDARD SQL DDL: SILVER RETAIL CUSTOMER DATA PRODUCT
-- Standards: BIAN Party Reference Data v12 / ISO 20022 / FIBO Party
-- Conformance: Tony Giordano "Conforming Logic" Methodology (Step 5 - Grain & History)
-- ==============================================================================

-- 1. CONFORMED GOLDEN CUSTOMER TABLE
-- Grain: Exactly one row per real-world unique customer entity
CREATE TABLE IF NOT EXISTS `{project_id}.{silver_dataset}.silver_customer` (
    -- Primary Key & Entity Classification
    customer_key           STRING NOT NULL OPTIONS(description="Deterministic surrogate primary key generated from resolved identity cluster."),
    customer_type          STRING NOT NULL OPTIONS(description="Conformed customer type from ref_crosswalk_customer_type: INDIVIDUAL | ORGANIZATION."),
    
    -- Legal Demographics & Name (Won by System of Record: Hogan Core KYC)
    legal_name             STRING NOT NULL OPTIONS(description="Standardized legal full name. Won by KYC system of record (Hogan)."),
    first_name             STRING OPTIONS(description="Cleaned, title-cased first name."),
    last_name              STRING OPTIONS(description="Cleaned, title-cased last name."),
    date_of_birth          DATE OPTIONS(description="ISO 8601 birth date (YYYY-MM-DD). Won by KYC system of record."),
    
    -- Identifiers (Normalized & Masked for PII Protection)
    tax_id_normalized      STRING OPTIONS(description="Cleaned 9-digit national tax identifier (SSN/EIN/TIN)."),
    tax_id_masked          STRING OPTIONS(description="Redacted identifier (***-**-1234) for governance and analytics."),
    
    -- Contact Points (Won by Most Recent Active Touchpoint: TSYS / ACLS)
    primary_phone_e164     STRING OPTIONS(description="Standardized E.164 phone number (+1XXXXXXXXXX)."),
    primary_email          STRING OPTIONS(description="Cleaned lowercase verified email address."),
    primary_postal_code    STRING OPTIONS(description="Standardized 5-digit postal zip code."),
    
    -- Status & Cross-System Lineage Metadata
    relationship_status    STRING NOT NULL OPTIONS(description="Conformed relationship status: ACTIVE | DORMANT | CLOSED | SUSPENDED."),
    survivorship_source    STRING NOT NULL OPTIONS(description="Primary system of record that won legal profile survivorship."),
    contributing_sources   ARRAY<STRING> NOT NULL OPTIONS(description="Array of all source systems contributing to this golden record (e.g. ['HOGAN','TSYS','ACLS'])."),
    source_record_count    INT64 NOT NULL OPTIONS(description="Total count of raw source records merged into this customer entity."),
    
    -- SCD Type-2 Technical Envelope
    effective_from_ts      TIMESTAMP NOT NULL OPTIONS(description="SCD Type-2 validity start timestamp."),
    effective_to_ts        TIMESTAMP OPTIONS(description="SCD Type-2 validity end timestamp (NULL if active)."),
    is_current             BOOL NOT NULL OPTIONS(description="True if record is the current active version."),
    dq_status              STRING NOT NULL OPTIONS(description="Data quality status: VALID | WARNING | QUARANTINE.")
)
PARTITION BY DATE(effective_from_ts)
CLUSTER BY customer_key, customer_type, relationship_status
OPTIONS(
    description = "Silver Conformed Customer Data Product conformed across Hogan, AFS, TSYS, and ACLS sources per Tony Giordano Conforming Logic framework.",
    labels = [("domain", "customer"), ("layer", "silver"), ("bian_domain", "party_reference_data")]
);

-- 2. CUSTOMER SOURCE SYSTEM CROSS-REFERENCE TABLE
-- Purpose: Preserves the path back to every source key for auditability and unmerging (Slide 4)
CREATE TABLE IF NOT EXISTS `{project_id}.{silver_dataset}.silver_customer_source_xref` (
    xref_key               STRING NOT NULL OPTIONS(description="Surrogate unique identifier for this cross-reference link."),
    customer_key           STRING NOT NULL OPTIONS(description="Foreign key referencing silver_customer.customer_key."),
    source_system          STRING NOT NULL OPTIONS(description="Originating source system: HOGAN | AFS | TSYS | ACLS."),
    source_entity          STRING NOT NULL OPTIONS(description="Bronze source dataset name (e.g. brz_hogan_ci_customer)."),
    source_customer_id     STRING NOT NULL OPTIONS(description="Original source system primary key (e.g. CI_CUST_NUM, cardholder_id, APPLICANT_ID)."),
    match_method           STRING NOT NULL OPTIONS(description="Identity resolution rule applied: EXACT_TAX_ID | COMPOSITE_PHONE_DOB | COMPOSITE_LASTNAME_PHONE."),
    match_score            NUMERIC NOT NULL OPTIONS(description="Confidence score of the match: 1.00 for deterministic, 0.85-0.95 for composite."),
    review_status          STRING NOT NULL OPTIONS(description="Review governance status: AUTO_APPROVED | STEWARD_REVIEW | APPROVED_BY_STEWARD."),
    linked_at              TIMESTAMP NOT NULL OPTIONS(description="Timestamp when the identity link was established.")
)
PARTITION BY DATE(linked_at)
CLUSTER BY customer_key, source_system, source_customer_id
OPTIONS(
    description = "Cross-reference link table maintaining bi-directional lineage from Silver Customer back to all Bronze source keys.",
    labels = [("domain", "customer"), ("layer", "silver"), ("type", "lineage_xref")]
);
