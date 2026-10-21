---
name: column-name-standardizer
description: Recommends a standardized physical name for every :Column node in a project's Neo4j knowledge graph. Applies snake_case + an optional domain prefix and expands common abbreviations (e.g. "cust_id" → "customer_customer_id" in the customer domain). Writes the result as :Column.recommendedName + :Column.recommendedNameStatus='pending_review'. Reviewed by the Data Product Owner in the dpe-sa po_source_validation gate. Use this skill whenever the user wants to "standardize column names", "recommend physical names", "rename columns for a source product", or as the column_name_standardization stage in the dpe-sa archetype. Idempotent — re-running over already-standardized columns is a no-op. Trigger on phrases "standardize names", "recommend column names", "snake_case columns", "name standardization".
---

# Column Name Standardizer

The dpe-sa source-aligned product flow needs every discovered :Column to
have a clean, consistent physical name before the PO is asked to validate
the product. The Product Owner shouldn't be reviewing 200 columns named
`cust_id`, `Cust_Id`, `CUSTID`, `CustomerId` — they should be reviewing
one consistent shape.

## What it does

For every :Column node in a project's Neo4j graph this skill writes:

- `col.recommendedName` — the standardized physical name candidate
- `col.recommendedNameStatus = 'pending_review'` — so the PO validation
  gate picks it up. (Status flips to `'approved'`, `'rejected'`, or stays
  unchanged when the PO edits.)

Idempotent. If `col.recommendedName` already exists and matches what the
skill would produce, it skips the column.

## Naming algorithm

1. **Tokenise** the source name on case boundaries, underscores, hyphens,
   and spaces. `customerID` → `[customer, id]`.
2. **Expand abbreviations** using the small built-in dictionary (`id`
   stays `id`, `cust` → `customer`, `qty` → `quantity`, `addr` → `address`,
   `pmt`/`pmnt` → `payment`, `ord` → `order`, `prod` → `product`, `inv` →
   `invoice`, `acct` → `account`, `amt` → `amount`, `dt`/`dttm` → `date`,
   `desc` → `description`, `nbr`/`num` → `number`).
3. **Lowercase** every token.
4. **Add a domain prefix** if `--domain` is supplied and the prefix isn't
   already the first token. `customer` domain + `address` → `customer_address`.
5. **Snake_case-join** the tokens.

Example outputs (with `--domain customer`):

| Source name      | Recommendation              |
|------------------|-----------------------------|
| `cust_id`        | `customer_id`               |
| `CustomerName`   | `customer_name`             |
| `addr_line_1`    | `customer_address_line_1`   |
| `pmt_amt`        | `customer_payment_amount`   |

When the domain word is already the first token after expansion (e.g.
`cust_id` → `customer_id`), the prefix is *not* doubled. This keeps
common names like `customer_id` clean rather than `customer_customer_id`.

When the source name already matches what the algorithm would produce,
the skill writes it anyway with status `'pending_review'` so the PO
sees an explicit "no change" suggestion.

## Inputs

- `--project-code <code>` (required) — scopes writes to that project's
  :Column nodes. Same convention as every other workbench skill.
- `--domain <slug>` (optional) — domain prefix; lowercased before use.
- `--host`, `--bolt-port`, `--username`, `--password`, `--database` —
  Neo4j connection. Defaults match the rest of the workbench
  (`localhost:7687` / `neo4j` / `your_password` / `neo4j`).

## Invocation

```bash
python ${CLAUDE_SKILL_DIR}/scripts/standardize_names.py \
  --project-code dpe-sa-05082026-01 \
  --domain customer \
  --host localhost --bolt-port 7687 \
  --username neo4j --password your_password \
  --database neo4j
```

Always pass `--project-code`. The script writes a small per-run summary
at the end:

```
Wrote 47 recommendations (12 already up-to-date, 0 errors)
```

## Forbidden

- Do not query :Column nodes from other projects. The script enforces
  this via the `{projectCode}` filter; do not attempt to bypass.
- Do not edit `col.name` directly — that's the source-of-truth physical
  name from data discovery and changing it would break mapping/lineage.
  Only write `recommendedName`.
- Do not re-run with different domain prefixes back-to-back trying to
  sweep — pick a single domain per project and let the PO review.
