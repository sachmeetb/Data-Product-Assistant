# Sample: `retail-banking` (brownfield estate)

A synthetic **retail bank** whose data lives in **six siloed source systems**, all loaded as
separate **schemas** in one Postgres database (`retail_banking`). It is purpose-built for the
**Connected-Estate → Feasibility → Product Assembly → Intake** journey: discover the estate
top-down, grade it against a catalog of *desired* data-product specs, and let Product Assembly
turn the scanned systems into proposed source-aligned products.

> Companion walkthrough: `docs/scenarios/brownfield-retail-banking.md`.

## Load it

```bash
dwb up --pg-sample retail-banking      # loads just this sample (auto-enables the postgres profile)
# or, with everything:
dwb up --with postgres
```

The loader creates the `retail_banking` database and runs `01_schema.sql → 02_seed.sql →
03_consumer_schema.sql` in order (idempotent — an existing DB is skipped). A **Quick connect**
entry `retail-banking-postgres` is registered for the connection form.

## The estate (six schemas = six source systems)

| schema | source system | tables |
|---|---|---|
| `crm` | Party / KYC master | `party`, `party_contact`, `kyc_verification` |
| `core_banking` | Deposit-account core | `account`, `account_balance`, `deposit_transaction` |
| `cards` | Card management | `card`, `card_limit`, `card_transaction` |
| `lending` | Loan origination/servicing | `loan`, `loan_repayment`, `loan_delinquency` |
| `payments` | Payments hub | `beneficiary`, `payment_instruction` |
| `ops_noise` | Ops backoffice (never maps) | `support_ticket`, `facility_asset` |
| `retailbank_views` | Serving target (exclude from scan) | *(empty; created by 03_)* |

~200 customers, ~300 deposit accounts, ~4k deposit transactions, ~250 cards, ~150 loans,
~180 beneficiaries. Small on purpose — fast to scan, profile and grade.

## Deliberate modelling texture (why it's shaped this way)

- **FKs are intra-schema only** (e.g. `account_balance → account`, `loan_repayment → loan`). The
  estate scan records these as `:REFERENCES` edges *inside* each system, so **Product Assembly
  clusters each source system into its own seam** — one proposed source-aligned product per schema.
- **No cross-schema FKs.** The systems link only by **shared business keys**: `customer_id`
  everywhere, `account_id` within `core_banking`, `card_id` within `cards`. `customer_id` is
  deliberately denormalised onto (almost) every table so the clusterer treats it as a *ubiquitous*
  (non-discriminating) key and does **not** collapse the three core systems into one blob.
- **Column names line up with the reference-spec attributes** (`customer_id`, `account_id`,
  `card_id`, `ledger_balance`, `credit_limit`, `loan_status`, …). The scan reads column **names +
  types** — **not** SQL `COMMENT`s — so names carry the map-match; run the estate **enrich** pass to
  generate + embed descriptions that sharpen the semantic match before grading.
- **`ops_noise` maps to nothing**, and there is **no wealth/investment data at all**, so a
  *Wealth Portfolio 360* reference spec correctly grades **absent** — the honest "we don't have
  this" signal in the stoplight.

## Which reference specs light up

The three retail-banking reference specs
(`workbench-skills/skills/data-product-feasibility-evaluator/reference/retail-banking/`) grade
against this estate as:

| spec | expected grade | draws on |
|---|---|---|
| `agg_customer_banking_360` | **assemblable** (the star) | `crm` + `core_banking` + `cards` |
| `agg_customer_lending_exposure` | **assemblable** | `crm` + `lending` (+ `cards`) |
| `cns_wealth_portfolio_360` | **absent** | *(no matching estate data)* |

Deterministic (id-arithmetic + `generate_series`, no `random()`); `ANALYZE` runs at the end of the
seed so a fresh scan reports real row/size volumetrics. Re-running `01/02` drops and rebuilds the
six source schemas; `retailbank_views` is preserved.
