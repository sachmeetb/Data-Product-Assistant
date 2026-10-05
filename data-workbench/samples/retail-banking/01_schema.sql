-- =============================================================================
-- Data Workbench sample dataset — Retail Banking (brownfield estate)
-- File 1 of 3: schema (six simulated source systems in one Postgres database)
--
-- Six schemas model six physically separate systems a real bank would run:
--   crm          — Party/KYC master (system-of-record for customer identity)
--   core_banking — Deposit-account core (accounts, balances, transactions)
--   cards        — Card management system (cards, limits, card transactions)
--   lending      — Loan origination/servicing (loans, repayments, delinquency)
--   payments     — Payments hub (payment instructions + beneficiaries)
--   ops_noise    — Operations backoffice (tickets, facilities) — deliberately
--                  UNRELATED to any banking product, so a wealth/investment
--                  reference spec correctly grades `absent` and this schema
--                  never map-matches.
--
-- Deliberate modelling texture (drives the Connected-Estate → feasibility →
-- Product Assembly demo):
--   * FOREIGN KEYS ARE INTRA-SCHEMA ONLY — child→parent inside each system
--     (e.g. account_balance→account). This is what makes the estate scan record
--     `:REFERENCES` edges INSIDE each system, so Product Assembly clusters each
--     source system into its own "seam" (one source-aligned product per schema).
--   * NO cross-schema FKs — the systems link only by shared BUSINESS KEYS
--     (customer_id everywhere; account_id within core_banking; card_id within
--     cards). customer_id is deliberately denormalised onto (almost) every
--     in-scope table so the clusterer treats it as ubiquitous (a non-
--     discriminating join key) and does NOT collapse the three systems into one.
--   * Column NAMES are chosen to line up with the reference-spec attribute names
--     (customer_id, account_id, card_id, ledger_balance, credit_limit, …) — the
--     estate scan reads column *names* + types (not SQL COMMENTs), so the names
--     are what carry the map-match. The `enrich` pass then generates + embeds
--     descriptions that sharpen the semantic match.
--
-- Re-running is destructive: the six source schemas are dropped and recreated.
-- The consumer-view schema (retailbank_views) is created separately by
-- 03_consumer_schema.sql and is preserved across reloads.
-- =============================================================================

DROP SCHEMA IF EXISTS crm          CASCADE;
DROP SCHEMA IF EXISTS core_banking CASCADE;
DROP SCHEMA IF EXISTS cards        CASCADE;
DROP SCHEMA IF EXISTS lending      CASCADE;
DROP SCHEMA IF EXISTS payments     CASCADE;
DROP SCHEMA IF EXISTS ops_noise    CASCADE;

CREATE SCHEMA crm;
CREATE SCHEMA core_banking;
CREATE SCHEMA cards;
CREATE SCHEMA lending;
CREATE SCHEMA payments;
CREATE SCHEMA ops_noise;

-- =============================================================================
-- crm — Party / KYC master (system-of-record for customer identity)
-- =============================================================================

-- ---------------------------------------------------------------------------
-- crm.party — master customer/party record
-- ---------------------------------------------------------------------------
CREATE TABLE crm.party (
    customer_id          bigint       PRIMARY KEY,
    full_name            varchar(120) NOT NULL,
    date_of_birth        date         NOT NULL,
    gender               varchar(1)   NOT NULL,
    email                varchar(120) NOT NULL UNIQUE,
    mobile_number        varchar(16)  NOT NULL,
    residential_address  varchar(240) NOT NULL,
    customer_segment     varchar(16)  NOT NULL,
    kyc_status           varchar(16)  NOT NULL,
    onboarded_date       date         NOT NULL
);
COMMENT ON TABLE  crm.party IS 'Master customer/party record; the system-of-record for identity, demographics and contact details.';
COMMENT ON COLUMN crm.party.customer_id         IS 'Unique internal identifier for the customer (primary key; shared business key across every source system).';
COMMENT ON COLUMN crm.party.full_name           IS 'Full legal name of the customer.';
COMMENT ON COLUMN crm.party.date_of_birth       IS 'Customer date of birth.';
COMMENT ON COLUMN crm.party.gender              IS 'Recorded gender (M/F, O for organisations).';
COMMENT ON COLUMN crm.party.email               IS 'Primary email address on file.';
COMMENT ON COLUMN crm.party.mobile_number       IS 'Primary mobile contact number.';
COMMENT ON COLUMN crm.party.residential_address IS 'Registered residential/mailing address.';
COMMENT ON COLUMN crm.party.customer_segment    IS 'Marketing/value segment (mass, affluent, premium, private, business).';
COMMENT ON COLUMN crm.party.kyc_status          IS 'Current Know-Your-Customer verification state (verified, pending, expired).';
COMMENT ON COLUMN crm.party.onboarded_date      IS 'Date the customer relationship was established.';

-- ---------------------------------------------------------------------------
-- crm.party_contact — additional contact points per party (child of party)
-- ---------------------------------------------------------------------------
CREATE TABLE crm.party_contact (
    contact_id     bigint      PRIMARY KEY,
    customer_id    bigint      NOT NULL REFERENCES crm.party(customer_id),
    contact_type   varchar(16) NOT NULL,
    contact_value  varchar(160) NOT NULL,
    is_primary     boolean     NOT NULL
);
COMMENT ON TABLE  crm.party_contact IS 'Additional contact points (email/phone/address) for a party.';
COMMENT ON COLUMN crm.party_contact.contact_id    IS 'Unique identifier for the contact record.';
COMMENT ON COLUMN crm.party_contact.customer_id   IS 'Customer this contact belongs to (FK to crm.party).';
COMMENT ON COLUMN crm.party_contact.contact_type  IS 'Kind of contact point (email, mobile, landline, address).';
COMMENT ON COLUMN crm.party_contact.contact_value IS 'The contact value itself.';
COMMENT ON COLUMN crm.party_contact.is_primary    IS 'Whether this is the customer''s primary contact of its type.';
CREATE INDEX idx_party_contact_customer ON crm.party_contact (customer_id);

-- ---------------------------------------------------------------------------
-- crm.kyc_verification — KYC verification events per party (child of party)
-- ---------------------------------------------------------------------------
CREATE TABLE crm.kyc_verification (
    kyc_id             bigint      PRIMARY KEY,
    customer_id        bigint      NOT NULL REFERENCES crm.party(customer_id),
    document_type      varchar(24) NOT NULL,
    kyc_status         varchar(16) NOT NULL,
    verification_date  date
);
COMMENT ON TABLE  crm.kyc_verification IS 'Know-Your-Customer verification events and their outcome per party.';
COMMENT ON COLUMN crm.kyc_verification.kyc_id            IS 'Unique identifier for the KYC verification record.';
COMMENT ON COLUMN crm.kyc_verification.customer_id      IS 'Customer being verified (FK to crm.party).';
COMMENT ON COLUMN crm.kyc_verification.document_type    IS 'Identity document used (passport, national_id, driving_licence, pan).';
COMMENT ON COLUMN crm.kyc_verification.kyc_status       IS 'Outcome of the verification (verified, pending, expired).';
COMMENT ON COLUMN crm.kyc_verification.verification_date IS 'Date the verification completed. NULL while pending.';
CREATE INDEX idx_kyc_verification_customer ON crm.kyc_verification (customer_id);

-- =============================================================================
-- core_banking — Deposit-account core (accounts, balances, transactions)
-- =============================================================================

-- ---------------------------------------------------------------------------
-- core_banking.account — deposit account master
-- ---------------------------------------------------------------------------
CREATE TABLE core_banking.account (
    account_id      bigint       PRIMARY KEY,
    customer_id     bigint       NOT NULL,
    product_type    varchar(24)  NOT NULL,
    account_status  varchar(16)  NOT NULL,
    currency        varchar(3)   NOT NULL,
    open_date       date         NOT NULL
);
COMMENT ON TABLE  core_banking.account IS 'Deposit account master (savings/current/overdraft), one row per account.';
COMMENT ON COLUMN core_banking.account.account_id     IS 'Unique identifier for the deposit account (primary key; shared business key within core banking).';
COMMENT ON COLUMN core_banking.account.customer_id    IS 'Owning customer (shared business key with crm.party — no FK, separate system).';
COMMENT ON COLUMN core_banking.account.product_type   IS 'Account product (savings, current, overdraft, salary).';
COMMENT ON COLUMN core_banking.account.account_status IS 'Operational state (active, dormant, closed, frozen).';
COMMENT ON COLUMN core_banking.account.currency       IS 'ISO currency code the account is denominated in.';
COMMENT ON COLUMN core_banking.account.open_date      IS 'Date the account was opened.';
CREATE INDEX idx_account_customer ON core_banking.account (customer_id);

-- ---------------------------------------------------------------------------
-- core_banking.account_balance — current balance per account (child of account)
-- ---------------------------------------------------------------------------
CREATE TABLE core_banking.account_balance (
    account_id         bigint        NOT NULL REFERENCES core_banking.account(account_id),
    customer_id        bigint        NOT NULL,
    balance_date       date          NOT NULL,
    ledger_balance     numeric(14,2) NOT NULL,
    available_balance  numeric(14,2) NOT NULL,
    PRIMARY KEY (account_id, balance_date)
);
COMMENT ON TABLE  core_banking.account_balance IS 'Point-in-time balance per deposit account (ledger vs available).';
COMMENT ON COLUMN core_banking.account_balance.account_id        IS 'Account the balance is for (FK to core_banking.account).';
COMMENT ON COLUMN core_banking.account_balance.customer_id       IS 'Owning customer (denormalised shared business key).';
COMMENT ON COLUMN core_banking.account_balance.balance_date      IS 'As-of date of the balance snapshot.';
COMMENT ON COLUMN core_banking.account_balance.ledger_balance    IS 'Ledger (book) balance of the account as of balance_date.';
COMMENT ON COLUMN core_banking.account_balance.available_balance IS 'Available (spendable) balance after holds as of balance_date.';

-- ---------------------------------------------------------------------------
-- core_banking.deposit_transaction — account transaction ledger (child of account)
-- ---------------------------------------------------------------------------
CREATE TABLE core_banking.deposit_transaction (
    txn_id         bigint        PRIMARY KEY,
    account_id     bigint        NOT NULL REFERENCES core_banking.account(account_id),
    customer_id    bigint        NOT NULL,
    amount         numeric(14,2) NOT NULL,
    currency       varchar(3)    NOT NULL,
    txn_timestamp  timestamp     NOT NULL,
    debit_credit   varchar(1)    NOT NULL,
    channel        varchar(12)   NOT NULL
);
COMMENT ON TABLE  core_banking.deposit_transaction IS 'Line-level deposit-account transaction ledger across all channels.';
COMMENT ON COLUMN core_banking.deposit_transaction.txn_id        IS 'Unique transaction identifier (primary key).';
COMMENT ON COLUMN core_banking.deposit_transaction.account_id    IS 'Account the transaction posted to (FK to core_banking.account).';
COMMENT ON COLUMN core_banking.deposit_transaction.customer_id   IS 'Owning customer (denormalised shared business key).';
COMMENT ON COLUMN core_banking.deposit_transaction.amount        IS 'Monetary value of the transaction.';
COMMENT ON COLUMN core_banking.deposit_transaction.currency      IS 'ISO currency code of the transaction.';
COMMENT ON COLUMN core_banking.deposit_transaction.txn_timestamp IS 'Date/time the transaction was recorded.';
COMMENT ON COLUMN core_banking.deposit_transaction.debit_credit  IS 'Debit/credit indicator (D or C).';
COMMENT ON COLUMN core_banking.deposit_transaction.channel       IS 'Origination channel (atm, pos, online, branch, upi).';
CREATE INDEX idx_deposit_txn_account  ON core_banking.deposit_transaction (account_id);
CREATE INDEX idx_deposit_txn_customer ON core_banking.deposit_transaction (customer_id);

-- =============================================================================
-- cards — Card management system (cards, limits, card transactions)
-- NOTE: cards intentionally carry customer_id but NOT account_id, so account_id
-- stays confined to core_banking and does not merge cards into that cluster.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- cards.card — issued card master
-- ---------------------------------------------------------------------------
CREATE TABLE cards.card (
    card_id       bigint        PRIMARY KEY,
    customer_id   bigint        NOT NULL,
    card_type     varchar(16)   NOT NULL,
    card_status   varchar(16)   NOT NULL,
    issue_date    date          NOT NULL,
    expiry_date   date          NOT NULL,
    credit_limit  numeric(12,2) NOT NULL
);
COMMENT ON TABLE  cards.card IS 'Issued payment card master (credit/debit), one row per card.';
COMMENT ON COLUMN cards.card.card_id      IS 'Unique identifier for the payment card (primary key; shared business key within cards).';
COMMENT ON COLUMN cards.card.customer_id  IS 'Cardholder (shared business key with crm.party — no FK, separate system).';
COMMENT ON COLUMN cards.card.card_type    IS 'Card product tier (classic, gold, platinum, corporate, debit).';
COMMENT ON COLUMN cards.card.card_status  IS 'Current card state (active, blocked, expired, hotlisted).';
COMMENT ON COLUMN cards.card.issue_date   IS 'Date the card was issued.';
COMMENT ON COLUMN cards.card.expiry_date  IS 'Date the card expires.';
COMMENT ON COLUMN cards.card.credit_limit IS 'Maximum credit available on the card (0 for debit cards).';
CREATE INDEX idx_card_customer ON cards.card (customer_id);

-- ---------------------------------------------------------------------------
-- cards.card_limit — per-card limits (child of card)
-- ---------------------------------------------------------------------------
CREATE TABLE cards.card_limit (
    card_id      bigint        NOT NULL REFERENCES cards.card(card_id),
    customer_id  bigint        NOT NULL,
    limit_type   varchar(24)   NOT NULL,
    limit_amount numeric(12,2) NOT NULL,
    PRIMARY KEY (card_id, limit_type)
);
COMMENT ON TABLE  cards.card_limit IS 'Per-card transactional/withdrawal limits by limit type.';
COMMENT ON COLUMN cards.card_limit.card_id      IS 'Card the limit applies to (FK to cards.card).';
COMMENT ON COLUMN cards.card_limit.customer_id  IS 'Cardholder (denormalised shared business key).';
COMMENT ON COLUMN cards.card_limit.limit_type   IS 'Kind of limit (daily_pos, daily_atm, online, contactless).';
COMMENT ON COLUMN cards.card_limit.limit_amount IS 'Amount of the limit.';

-- ---------------------------------------------------------------------------
-- cards.card_transaction — card transaction ledger (child of card)
-- ---------------------------------------------------------------------------
CREATE TABLE cards.card_transaction (
    card_txn_id    bigint        PRIMARY KEY,
    card_id        bigint        NOT NULL REFERENCES cards.card(card_id),
    customer_id    bigint        NOT NULL,
    amount         numeric(12,2) NOT NULL,
    merchant_name  varchar(80)   NOT NULL,
    txn_date       date          NOT NULL,
    txn_type       varchar(16)   NOT NULL
);
COMMENT ON TABLE  cards.card_transaction IS 'Line-level card transaction ledger (purchases, cash advances, refunds).';
COMMENT ON COLUMN cards.card_transaction.card_txn_id   IS 'Unique identifier for the card transaction (primary key).';
COMMENT ON COLUMN cards.card_transaction.card_id       IS 'Card the transaction was made on (FK to cards.card).';
COMMENT ON COLUMN cards.card_transaction.customer_id   IS 'Cardholder (denormalised shared business key).';
COMMENT ON COLUMN cards.card_transaction.amount        IS 'Monetary value of the card transaction.';
COMMENT ON COLUMN cards.card_transaction.merchant_name IS 'Name of the merchant where the transaction occurred.';
COMMENT ON COLUMN cards.card_transaction.txn_date      IS 'Date the card transaction was processed.';
COMMENT ON COLUMN cards.card_transaction.txn_type      IS 'Type of card transaction (purchase, cash_advance, refund, reversal).';
CREATE INDEX idx_card_txn_card ON cards.card_transaction (card_id);

-- =============================================================================
-- lending — Loan origination/servicing (loans, repayments, delinquency)
-- =============================================================================

-- ---------------------------------------------------------------------------
-- lending.loan — loan master
-- ---------------------------------------------------------------------------
CREATE TABLE lending.loan (
    loan_id          bigint        PRIMARY KEY,
    customer_id      bigint        NOT NULL,
    loan_type        varchar(24)   NOT NULL,
    principal_amount numeric(14,2) NOT NULL,
    interest_rate    numeric(6,3)  NOT NULL,
    disbursal_date   date          NOT NULL,
    maturity_date    date          NOT NULL,
    loan_status      varchar(16)   NOT NULL
);
COMMENT ON TABLE  lending.loan IS 'Loan master (home/auto/personal/business), one row per loan account.';
COMMENT ON COLUMN lending.loan.loan_id          IS 'Unique identifier for the loan account (primary key; shared business key within lending).';
COMMENT ON COLUMN lending.loan.customer_id      IS 'Borrowing customer (shared business key with crm.party — no FK, separate system).';
COMMENT ON COLUMN lending.loan.loan_type        IS 'Loan product (home, auto, personal, business, education).';
COMMENT ON COLUMN lending.loan.principal_amount IS 'Original sanctioned principal amount of the loan.';
COMMENT ON COLUMN lending.loan.interest_rate    IS 'Annual interest rate applied to the loan (percent).';
COMMENT ON COLUMN lending.loan.disbursal_date   IS 'Date the loan principal was disbursed.';
COMMENT ON COLUMN lending.loan.maturity_date    IS 'Contractual maturity date of the loan.';
COMMENT ON COLUMN lending.loan.loan_status      IS 'Servicing state (active, closed, written_off, delinquent).';
CREATE INDEX idx_loan_customer ON lending.loan (customer_id);

-- ---------------------------------------------------------------------------
-- lending.loan_repayment — scheduled/actual repayments (child of loan)
-- ---------------------------------------------------------------------------
CREATE TABLE lending.loan_repayment (
    repayment_id  bigint        PRIMARY KEY,
    loan_id       bigint        NOT NULL REFERENCES lending.loan(loan_id),
    customer_id   bigint        NOT NULL,
    due_date      date          NOT NULL,
    amount_due    numeric(12,2) NOT NULL,
    amount_paid   numeric(12,2) NOT NULL,
    paid_date     date
);
COMMENT ON TABLE  lending.loan_repayment IS 'Scheduled instalment repayments and their settlement per loan.';
COMMENT ON COLUMN lending.loan_repayment.repayment_id IS 'Unique identifier for the repayment schedule row (primary key).';
COMMENT ON COLUMN lending.loan_repayment.loan_id      IS 'Loan the repayment belongs to (FK to lending.loan).';
COMMENT ON COLUMN lending.loan_repayment.customer_id  IS 'Borrowing customer (denormalised shared business key).';
COMMENT ON COLUMN lending.loan_repayment.due_date     IS 'Date the instalment is due.';
COMMENT ON COLUMN lending.loan_repayment.amount_due   IS 'Instalment amount due.';
COMMENT ON COLUMN lending.loan_repayment.amount_paid  IS 'Instalment amount actually paid.';
COMMENT ON COLUMN lending.loan_repayment.paid_date    IS 'Date the instalment was paid. NULL if unpaid.';
CREATE INDEX idx_loan_repayment_loan ON lending.loan_repayment (loan_id);

-- ---------------------------------------------------------------------------
-- lending.loan_delinquency — current delinquency status per loan (child of loan)
-- ---------------------------------------------------------------------------
CREATE TABLE lending.loan_delinquency (
    loan_id         bigint        NOT NULL REFERENCES lending.loan(loan_id),
    customer_id     bigint        NOT NULL,
    as_of_date      date          NOT NULL,
    dpd_bucket      varchar(12)   NOT NULL,
    overdue_amount  numeric(12,2) NOT NULL,
    PRIMARY KEY (loan_id, as_of_date)
);
COMMENT ON TABLE  lending.loan_delinquency IS 'Current delinquency / days-past-due status per loan.';
COMMENT ON COLUMN lending.loan_delinquency.loan_id        IS 'Loan the delinquency status is for (FK to lending.loan).';
COMMENT ON COLUMN lending.loan_delinquency.customer_id    IS 'Borrowing customer (denormalised shared business key).';
COMMENT ON COLUMN lending.loan_delinquency.as_of_date     IS 'As-of date of the delinquency snapshot.';
COMMENT ON COLUMN lending.loan_delinquency.dpd_bucket     IS 'Days-past-due bucket (current, 1-30, 31-60, 61-90, 90+).';
COMMENT ON COLUMN lending.loan_delinquency.overdue_amount IS 'Total overdue amount as of as_of_date.';

-- =============================================================================
-- payments — Payments hub (payment instructions + beneficiaries)
-- =============================================================================

-- ---------------------------------------------------------------------------
-- payments.beneficiary — saved payees per customer
-- ---------------------------------------------------------------------------
CREATE TABLE payments.beneficiary (
    beneficiary_id    bigint       PRIMARY KEY,
    customer_id       bigint       NOT NULL,
    beneficiary_name  varchar(120) NOT NULL,
    account_number    varchar(24)  NOT NULL,
    ifsc              varchar(16)  NOT NULL
);
COMMENT ON TABLE  payments.beneficiary IS 'Saved payees/beneficiaries registered by a customer for outbound payments.';
COMMENT ON COLUMN payments.beneficiary.beneficiary_id   IS 'Unique identifier for the beneficiary (primary key).';
COMMENT ON COLUMN payments.beneficiary.customer_id      IS 'Customer who registered the beneficiary (shared business key with crm.party).';
COMMENT ON COLUMN payments.beneficiary.beneficiary_name IS 'Name of the payee.';
COMMENT ON COLUMN payments.beneficiary.account_number   IS 'Destination account number of the payee.';
COMMENT ON COLUMN payments.beneficiary.ifsc             IS 'Bank branch routing code (IFSC) of the payee account.';
CREATE INDEX idx_beneficiary_customer ON payments.beneficiary (customer_id);

-- ---------------------------------------------------------------------------
-- payments.payment_instruction — outbound payment instructions (child of beneficiary)
-- ---------------------------------------------------------------------------
CREATE TABLE payments.payment_instruction (
    payment_id     bigint        PRIMARY KEY,
    customer_id    bigint        NOT NULL,
    beneficiary_id bigint        NOT NULL REFERENCES payments.beneficiary(beneficiary_id),
    amount         numeric(14,2) NOT NULL,
    currency       varchar(3)    NOT NULL,
    status         varchar(16)   NOT NULL,
    value_date     date          NOT NULL
);
COMMENT ON TABLE  payments.payment_instruction IS 'Outbound payment instructions submitted by customers to registered beneficiaries.';
COMMENT ON COLUMN payments.payment_instruction.payment_id     IS 'Unique identifier for the payment instruction (primary key).';
COMMENT ON COLUMN payments.payment_instruction.customer_id    IS 'Customer who initiated the payment (shared business key with crm.party).';
COMMENT ON COLUMN payments.payment_instruction.beneficiary_id IS 'Payee the payment is directed to (FK to payments.beneficiary).';
COMMENT ON COLUMN payments.payment_instruction.amount         IS 'Monetary value of the payment.';
COMMENT ON COLUMN payments.payment_instruction.currency       IS 'ISO currency code of the payment.';
COMMENT ON COLUMN payments.payment_instruction.status         IS 'Processing state (submitted, settled, returned, failed).';
COMMENT ON COLUMN payments.payment_instruction.value_date     IS 'Value date the payment settles on.';
CREATE INDEX idx_payment_instruction_beneficiary ON payments.payment_instruction (beneficiary_id);

-- =============================================================================
-- ops_noise — Operations backoffice. Deliberately UNRELATED to banking products
-- so a wealth/investment reference spec grades `absent` and this schema never
-- map-matches a customer/account/card/loan product.
-- =============================================================================

CREATE TABLE ops_noise.support_ticket (
    ticket_id  bigint      PRIMARY KEY,
    subject    varchar(160) NOT NULL,
    priority   varchar(8)  NOT NULL,
    status     varchar(16) NOT NULL,
    opened_at  timestamp   NOT NULL
);
COMMENT ON TABLE  ops_noise.support_ticket IS 'Internal IT/operations support tickets (backoffice; unrelated to customer banking products).';
COMMENT ON COLUMN ops_noise.support_ticket.ticket_id IS 'Unique identifier for the support ticket.';
COMMENT ON COLUMN ops_noise.support_ticket.subject   IS 'Short description of the issue.';
COMMENT ON COLUMN ops_noise.support_ticket.priority  IS 'Ticket priority (low, medium, high, urgent).';
COMMENT ON COLUMN ops_noise.support_ticket.status    IS 'Ticket state (open, in_progress, resolved, closed).';
COMMENT ON COLUMN ops_noise.support_ticket.opened_at IS 'Timestamp the ticket was opened.';

CREATE TABLE ops_noise.facility_asset (
    asset_id   bigint      PRIMARY KEY,
    asset_type varchar(24) NOT NULL,
    location   varchar(80) NOT NULL,
    status     varchar(16) NOT NULL
);
COMMENT ON TABLE  ops_noise.facility_asset IS 'Physical facility assets (ATMs, servers, branch equipment; backoffice inventory).';
COMMENT ON COLUMN ops_noise.facility_asset.asset_id   IS 'Unique identifier for the facility asset.';
COMMENT ON COLUMN ops_noise.facility_asset.asset_type IS 'Kind of asset (atm, server, printer, vehicle).';
COMMENT ON COLUMN ops_noise.facility_asset.location   IS 'Physical location of the asset.';
COMMENT ON COLUMN ops_noise.facility_asset.status     IS 'Operational state (in_service, maintenance, retired).';
