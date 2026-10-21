-- =============================================================================
-- Data Workbench sample dataset — Banking / Customer domain
-- File 1 of 3: schema (three simulated platforms in one Postgres database)
--
-- Four schemas model four physically separate systems:
--   edw        — Enterprise DW (simulates Teradata): customer master +
--                campaign response ledger
--   retail     — Retail banking data lake (simulates Hive): customer profile
--   analytics  — Analytics lake (simulates Hive): transaction ledger
--   marketing  — Marketing team's published mart: campaign_audience
--                (campaign responses pre-joined with the customer segment)
--
-- Deliberate modelling texture (good discovery/mapping demo material):
--   * NO cross-schema foreign keys — the systems are linked only by the
--     shared business key `customer_id`, which the consumer layer must
--     reconcile itself (mirrors the hr_core/hr_comp pattern).
--   * The Hive-simulated tables (retail.*, analytics.*) declare NO foreign
--     keys at all — Hive has no FK enforcement; relationships must be
--     inferred from data.
--   * retail.customer_profile overlaps edw.customer on identity/contact
--     columns (fully consistent values — same system-of-record feed).
--
-- Re-running is destructive: all three schemas are dropped and recreated.
-- The consumer-view schema (banking_views) is created separately by
-- 03_consumer_schema.sql and is preserved across reloads.
-- =============================================================================

DROP SCHEMA IF EXISTS edw CASCADE;
DROP SCHEMA IF EXISTS retail CASCADE;
DROP SCHEMA IF EXISTS analytics CASCADE;
DROP SCHEMA IF EXISTS marketing CASCADE;

CREATE SCHEMA edw;
CREATE SCHEMA retail;
CREATE SCHEMA analytics;
CREATE SCHEMA marketing;

-- ---------------------------------------------------------------------------
-- edw.customer — master customer record (system-of-record for identity/KYC)
-- ---------------------------------------------------------------------------
CREATE TABLE edw.customer (
    customer_id            bigint       PRIMARY KEY,
    name                   varchar(120) NOT NULL,
    gender                 varchar(1)   NOT NULL,
    pan_and_aadhaar_number varchar(24)  NOT NULL UNIQUE,
    customer_type          varchar(16)  NOT NULL,
    email                  varchar(120) NOT NULL UNIQUE,
    mobile_number          varchar(16)  NOT NULL,
    address                varchar(240) NOT NULL,
    kyc_status             varchar(16)  NOT NULL,
    onboarded_date         date         NOT NULL,
    customer_segment       varchar(16)  NOT NULL
);
COMMENT ON TABLE  edw.customer IS 'Master customer record for all banked parties; the system-of-record for identity, KYC, and contact details.';
COMMENT ON COLUMN edw.customer.customer_id            IS 'Unique internal identifier for the customer (primary key).';
COMMENT ON COLUMN edw.customer.name                   IS 'Full legal name of the customer.';
COMMENT ON COLUMN edw.customer.gender                 IS 'Customer''s recorded gender (M/F, O for organisations).';
COMMENT ON COLUMN edw.customer.pan_and_aadhaar_number IS 'Government-issued PAN/Aadhaar identifier (PII-sensitive).';
COMMENT ON COLUMN edw.customer.customer_type          IS 'Segment classification (retail, corporate, hni).';
COMMENT ON COLUMN edw.customer.email                  IS 'Primary email address on file.';
COMMENT ON COLUMN edw.customer.mobile_number          IS 'Primary mobile contact number (+91 format).';
COMMENT ON COLUMN edw.customer.address                IS 'Registered mailing/residential address.';
COMMENT ON COLUMN edw.customer.kyc_status             IS 'Current Know-Your-Customer verification state (verified, pending, expired).';
COMMENT ON COLUMN edw.customer.onboarded_date         IS 'Date the customer relationship was established.';
COMMENT ON COLUMN edw.customer.customer_segment       IS 'Marketing/value segment (mass, affluent, premium, private, business).';

-- ---------------------------------------------------------------------------
-- edw.campaign_response — campaign send/response ledger (one row per
-- customer × campaign message)
-- ---------------------------------------------------------------------------
CREATE TABLE edw.campaign_response (
    customer_id       bigint        NOT NULL REFERENCES edw.customer(customer_id),
    campaign_id       bigint        NOT NULL,
    conversion_status varchar(16)   NOT NULL,
    conversion_value  numeric(12,2),
    message_sent_date date          NOT NULL,
    channel           varchar(12)   NOT NULL,
    opened_flag       boolean       NOT NULL,
    PRIMARY KEY (campaign_id, customer_id)
);
COMMENT ON TABLE  edw.campaign_response IS 'Marketing campaign send/response ledger; one row per customer per campaign message.';
COMMENT ON COLUMN edw.campaign_response.customer_id       IS 'Customer the message was sent to (FK to edw.customer).';
COMMENT ON COLUMN edw.campaign_response.campaign_id       IS 'Identifier of the marketing campaign.';
COMMENT ON COLUMN edw.campaign_response.conversion_status IS 'Response outcome (converted, engaged, no_response, unsubscribed).';
COMMENT ON COLUMN edw.campaign_response.conversion_value  IS 'Monetary value attributed to the conversion. NULL unless conversion_status = converted.';
COMMENT ON COLUMN edw.campaign_response.message_sent_date IS 'Date the campaign message was sent.';
COMMENT ON COLUMN edw.campaign_response.channel           IS 'Delivery channel of the message (email, sms, whatsapp, push).';
COMMENT ON COLUMN edw.campaign_response.opened_flag       IS 'Whether the customer opened/viewed the message.';

CREATE INDEX idx_campaign_response_customer ON edw.campaign_response (customer_id);

-- ---------------------------------------------------------------------------
-- retail.customer_profile — retail data-lake customer profile (Hive-sim:
-- no FKs; overlaps edw.customer, consistent values)
-- ---------------------------------------------------------------------------
CREATE TABLE retail.customer_profile (
    customer_id                bigint       PRIMARY KEY,
    name                       varchar(120) NOT NULL,
    gender                     varchar(1)   NOT NULL,
    customer_type              varchar(16)  NOT NULL,
    citizenship_country        varchar(2)   NOT NULL,
    onboarding_channel         varchar(24)  NOT NULL,
    email                      varchar(120) NOT NULL,
    mobile_number              varchar(16)  NOT NULL,
    communication_channel_pref varchar(12)  NOT NULL,
    marketing_opt_in_flag      boolean      NOT NULL,
    kyc_status                 varchar(16)  NOT NULL,
    kyc_verification_date      date,
    onboarded_date             date         NOT NULL
);
COMMENT ON TABLE  retail.customer_profile IS 'Retail data-lake customer profile; overlaps the EDW customer master (consistent feed) and adds consent/preference attributes.';
COMMENT ON COLUMN retail.customer_profile.customer_id                IS 'Enterprise customer identifier (shared business key with edw.customer — no FK, different platform).';
COMMENT ON COLUMN retail.customer_profile.citizenship_country        IS 'ISO 3166-1 alpha-2 citizenship country code.';
COMMENT ON COLUMN retail.customer_profile.onboarding_channel         IS 'Channel through which the customer was onboarded (branch, online, mobile_app, relationship_manager).';
COMMENT ON COLUMN retail.customer_profile.communication_channel_pref IS 'Customer''s preferred outreach channel (email, sms, whatsapp, phone).';
COMMENT ON COLUMN retail.customer_profile.marketing_opt_in_flag      IS 'Whether the customer has opted in to marketing communications.';
COMMENT ON COLUMN retail.customer_profile.kyc_status                 IS 'KYC verification state as known to the retail platform (verified, pending, expired).';
COMMENT ON COLUMN retail.customer_profile.kyc_verification_date      IS 'Date KYC was last verified. NULL while verification is pending.';

-- ---------------------------------------------------------------------------
-- analytics.transaction_ledger — analytics-lake transaction ledger (Hive-sim:
-- no FKs; references accounts/customers by id only)
-- ---------------------------------------------------------------------------
CREATE TABLE analytics.transaction_ledger (
    txn_id        bigint        PRIMARY KEY,
    account_id    bigint        NOT NULL,
    customer_id   bigint        NOT NULL,
    amount        numeric(14,2) NOT NULL,
    currency      varchar(3)    NOT NULL,
    txn_timestamp timestamp     NOT NULL,
    channel       varchar(12)   NOT NULL,
    status        varchar(12)   NOT NULL,
    merchant_id   varchar(16),
    mcc           varchar(4),
    debit_credit  varchar(1)    NOT NULL,
    gl_account    varchar(12)   NOT NULL,
    source_system varchar(12)   NOT NULL
);
COMMENT ON TABLE  analytics.transaction_ledger IS 'Line-level financial transaction ledger landed in the analytics lake across all channels and source systems.';
COMMENT ON COLUMN analytics.transaction_ledger.txn_id        IS 'Unique transaction identifier (primary key).';
COMMENT ON COLUMN analytics.transaction_ledger.account_id    IS 'Account the transaction posted to (account master not in scope for this lake).';
COMMENT ON COLUMN analytics.transaction_ledger.customer_id   IS 'Customer who owns the transaction (shared business key with edw.customer — no FK, different platform).';
COMMENT ON COLUMN analytics.transaction_ledger.amount        IS 'Monetary value of the transaction.';
COMMENT ON COLUMN analytics.transaction_ledger.currency      IS 'ISO currency code of the transaction.';
COMMENT ON COLUMN analytics.transaction_ledger.txn_timestamp IS 'Date/time the transaction was recorded.';
COMMENT ON COLUMN analytics.transaction_ledger.channel       IS 'Origination channel (atm, pos, online, branch, upi).';
COMMENT ON COLUMN analytics.transaction_ledger.status        IS 'Posting state (posted, pending, reversed).';
COMMENT ON COLUMN analytics.transaction_ledger.merchant_id   IS 'Identifier of the merchant, where applicable. NULL for atm/branch transactions.';
COMMENT ON COLUMN analytics.transaction_ledger.mcc           IS 'Merchant Category Code classifying the merchant''s business. NULL when merchant_id is NULL.';
COMMENT ON COLUMN analytics.transaction_ledger.debit_credit  IS 'Debit/credit indicator (D or C).';
COMMENT ON COLUMN analytics.transaction_ledger.gl_account    IS 'General-ledger account code the transaction posts to.';
COMMENT ON COLUMN analytics.transaction_ledger.source_system IS 'Originating source system (CBS, UPI-GW, CARDS).';

CREATE INDEX idx_txn_ledger_customer  ON analytics.transaction_ledger (customer_id);
CREATE INDEX idx_txn_ledger_account   ON analytics.transaction_ledger (account_id);
CREATE INDEX idx_txn_ledger_timestamp ON analytics.transaction_ledger (txn_timestamp);

-- ---------------------------------------------------------------------------
-- marketing.campaign_audience — team-published mart: campaign responses
-- pre-joined with the customer's marketing segment. Derived downstream of
-- edw.campaign_response + edw.customer; published as its OWN dataset by the
-- marketing team (no FKs back to the EDW — separate platform boundary).
-- ---------------------------------------------------------------------------
CREATE TABLE marketing.campaign_audience (
    customer_id       bigint        NOT NULL,
    customer_segment  varchar(16)   NOT NULL,
    campaign_id       bigint        NOT NULL,
    conversion_status varchar(16)   NOT NULL,
    conversion_value  numeric(12,2),
    message_sent_date date          NOT NULL,
    channel           varchar(12)   NOT NULL,
    opened_flag       boolean       NOT NULL,
    PRIMARY KEY (campaign_id, customer_id)
);
COMMENT ON TABLE  marketing.campaign_audience IS 'Marketing-team mart: campaign send/response facts enriched with the customer''s marketing segment. One row per customer per campaign message.';
COMMENT ON COLUMN marketing.campaign_audience.customer_id       IS 'Enterprise customer identifier (shared business key with edw.customer — no FK, published mart).';
COMMENT ON COLUMN marketing.campaign_audience.customer_segment  IS 'Marketing/value segment of the customer at publish time (from the EDW master).';
COMMENT ON COLUMN marketing.campaign_audience.campaign_id       IS 'Identifier of the marketing campaign.';
COMMENT ON COLUMN marketing.campaign_audience.conversion_status IS 'Response outcome (converted, engaged, no_response, unsubscribed).';
COMMENT ON COLUMN marketing.campaign_audience.conversion_value  IS 'Monetary value attributed to the conversion. NULL unless conversion_status = converted.';
COMMENT ON COLUMN marketing.campaign_audience.message_sent_date IS 'Date the campaign message was sent.';
COMMENT ON COLUMN marketing.campaign_audience.channel           IS 'Delivery channel of the message (email, sms, whatsapp, push).';
COMMENT ON COLUMN marketing.campaign_audience.opened_flag       IS 'Whether the customer opened/viewed the message.';

CREATE INDEX idx_campaign_audience_customer ON marketing.campaign_audience (customer_id);
