# Banking / Customer-domain sample dataset

A multi-platform retail-banking sample for the Data Workbench, in the
**customer** domain. Five tables across four simulated platforms — a
Teradata-style EDW, two Hive-style lakes, and a marketing team's published
mart — each meant to be onboarded as a **source-aligned data product**. On top
of those, the intended demo builds a **consumer-aligned Customer 360**
product that computes a churn-risk score from the raw ledger *inside the
product definition* (no pre-baked score anywhere in the sources).

Lives in its **own database** (`banking_demo`) alongside the other samples'
databases in the same `pgdemo` container.

## Source tables → source-aligned products

| Schema | Simulates | Table | Rows | Source product |
|---|---|---|---|---|
| `edw` | Teradata EDW | `customer` — identity/KYC master (+ `customer_segment`) | 500 | EDW Customer |
| `edw` | Teradata EDW | `campaign_response` — one row per customer × campaign message | 3,000 | (same product) |
| `retail` | Hive lake | `customer_profile` — profile + consent/preference attributes | 500 | Retail Profile |
| `analytics` | Hive lake | `transaction_ledger` — line-level transactions | 15,600 | Transaction Ledger |
| `marketing` | Team mart | `campaign_audience` — campaign_response ⨝ customer segment | 3,000 | Campaign Audience |

Create **one Workbench source project per schema** (4 projects → 4 source
products), connection string per project:

```
postgresql://postgres:demo@localhost:5433/banking_demo
```

(from the backend Docker container use `host.docker.internal` instead of
`localhost`). Suggested domain: **`customer`** — pairs with
`playbook/domain_catalogs/customer.yaml`.

**Cross-platform texture (deliberate):**
- **No cross-schema FKs.** Platforms link only via the shared business key
  `customer_id`. The Hive-simulated and mart tables declare no FKs at all;
  only inside `edw` is there a declared FK
  (`campaign_response.customer_id → customer.customer_id`).
- **`retail.customer_profile` overlaps `edw.customer`** on
  name/gender/customer_type/email/mobile/kyc_status/onboarded_date with
  **fully consistent values**, plus lake-only attributes:
  `citizenship_country`, `onboarding_channel`, `communication_channel_pref`,
  `marketing_opt_in_flag`, `kyc_verification_date`.
- **`marketing.campaign_audience` is derived** from
  `edw.campaign_response ⨝ edw.customer` (adds `customer_segment` to the
  response facts) — lineage the Workbench should recover during
  discovery/mapping even though the mart carries no FKs.

## Target consumer product: Customer 360

Consumer-aligned, built on the source products above (primarily
**Campaign Audience** + the raw customer/profile/ledger products). Attributes:

| Attribute | Kind | Source product |
|---|---|---|
| customer_id, customer_segment, campaign_id, conversion_status, conversion_value, message_sent_date | DIRECT | campaign_audience |
| name, gender, customer_type, email, mobile_number, kyc_status, onboarded_date | DIRECT | edw.customer / retail.customer_profile |
| churn_risk_score | **DERIVED** | computed in-product from analytics.transaction_ledger |
| ~~channel~~ | — | deliberately not in the product |

**Computing churn_risk_score inside the product definition** (no mart):

1. `last_txn_date` — `lookup` transform, `selection_strategy: 'aggregate'`,
   `aggregate_function: MAX` over `transaction_ledger.txn_timestamp`, keyed
   on `customer_id`.
2. `txn_count_90d` — same lookup shape with `COUNT` +
   `filter_clause: "txn_timestamp >= CURRENT_DATE - 90"`.
3. `churn_risk_score` — derived expression over those two product columns
   (`depends_on_product_columns`), rendered in the enriched CTE layer:

```sql
GREATEST(0, LEAST(100,
    (CURRENT_DATE - last_txn_date::date) * 2   -- 2 pts per day inactive
  - txn_count_90d * 5))                        -- minus 5 per recent txn
-- 0 = active … 100 = almost certainly churned
```

The seed shapes the ledger so the score is meaningful (cohorts fixed by
`customer_id`): **churned** (`customer_id % 7 = 3`, 72 customers — silent for
≥91 days → score 100), **declining** (`customer_id % 11 = 5`, ~39 customers →
mid scores), **active** (the rest → score 0). The seed's final query prints
the live band distribution (e.g. 370 / 33 / 21 / 76).

## Loading

Assumes the `pgdemo` container is running (port 5433). If not:

```bash
docker run -d --name pgdemo -p 5433:5432 -e POSTGRES_PASSWORD=demo postgres:16
```

Create the database (first time only), then load in order:

```bash
docker exec pgdemo psql -U postgres -c "CREATE DATABASE banking_demo"

psql "postgresql://postgres:demo@localhost:5433/banking_demo" -f samples/banking/01_schema.sql
psql "postgresql://postgres:demo@localhost:5433/banking_demo" -f samples/banking/02_seed.sql
psql "postgresql://postgres:demo@localhost:5433/banking_demo" -f samples/banking/03_consumer_schema.sql
```

No host `psql`? Pipe through the container: `docker exec -i pgdemo psql -U postgres -d banking_demo < samples/banking/01_schema.sql` (etc.).

Re-running `01` + `02` is destructive to the `edw` / `retail` / `analytics` /
`marketing` schemas only. `banking_views` (deployed product views) survives
reloads.

**Determinism note:** everything is id-arithmetic (no `random()`). One
deliberate deviation: `analytics.transaction_ledger` timestamps anchor to
`CURRENT_DATE` so the churn recency signal stays live whenever the demo is
run — reloads are identical within a day and cohort membership is identical
always, but timestamps shift day to day.

## Verification

`02_seed.sql` ends with integrity checks (`RAISE EXCEPTION` on any violation
— row-count floors, profile↔master consistency, mart↔response coherence,
churned-cohort silence, conversion-field coherence), a volume summary, and a
churn-band preview. Expected counts:

| Table | Rows |
|---|---|
| `edw.customer` | 500 |
| `edw.campaign_response` | 3,000 |
| `retail.customer_profile` | 500 |
| `analytics.transaction_ledger` | 15,600 |
| `marketing.campaign_audience` | 3,000 |

## Data characteristics

- **Customers (500):** 450 retail / 40 corporate / 10 HNI. Segments: mass 266 /
  affluent 134 / premium 50 / business 40 / private 10. KYC: 425 verified /
  50 pending / 25 expired. Indian names/addresses, synthetic PAN-format IDs,
  +91 mobiles.
- **Campaign responses (3,000; mirrored into the mart):** 12 campaigns
  (ids 101–112, Apr 2025 → Jun 2026), each targeting a deterministic ~40%
  audience. Outcomes: no_response 1,870 / engaged 750 / converted 230 /
  unsubscribed 150. `conversion_value` ₹511–4,962 only on converted rows;
  converted always implies `opened_flag`.
- **Transaction ledger (15,600):** 800 synthetic accounts (no account master
  in scope), owner = fixed function of `account_id`, every customer covered.
  Channels weighted upi > pos > online > atm > branch; `merchant_id`+`mcc`
  only on pos/online/upi; `debit_credit` ~70% D; `gl_account` by channel;
  `source_system` CBS / UPI-GW / CARDS; status posted ~96% / pending ~3% /
  reversed ~1%.
- **PII surface:** `pan_and_aadhaar_number`, `mobile_number`, `email`,
  `address`, `name`, `citizenship_country`.

## Demo coverage

| Capability | Where it bites |
|---|---|
| Multi-platform discovery | 4 schemas as 4 systems; Hive-sim + mart tables have no FKs |
| Cross-system reconciliation | edw.customer ↔ retail.customer_profile on the shared business key |
| Mart lineage recovery | campaign_audience derived from campaign_response ⨝ customer |
| In-product derived columns | churn_risk_score via lookup-aggregates + derived expression (recipe above) |
| Consent gating | marketing_opt_in_flag / communication_channel_pref for audiences |
| DQ rules | KYC enums, PAN regex, conversion-field coherence, merchant/mcc pairing (see `customer.yaml`) |
| PII handling | mask/hash on PAN, mobile, email |

## Companion files

- `playbook/domain_catalogs/customer.yaml` — the `customer` domain catalog,
  covering this sample's columns (identity/PII, campaign response,
  consent/preference, churn score 0–100).
