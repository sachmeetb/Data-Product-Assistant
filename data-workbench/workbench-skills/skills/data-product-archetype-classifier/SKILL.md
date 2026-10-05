---
name: data-product-archetype-classifier
description: Classifies an ODCS data product spec as source-aligned or consumer-aligned, ranks marketplace source products against a consumer's input slot, and synthesizes a minimal source-product seed when no good match exists. Pure-text skill — invoked programmatically from POST /api/ingest-products/classify-archetype and /api/ingest-products/match-inputs. Operates in three modes selected by the caller's `MODE:` header: `classify_archetype`, `match_inputs`, `synthesize_missing_source`.
---

# Data Product Archetype Classifier

You serve three closely-related tasks for the consumer-product ingest flow, dispatched by the `MODE:` header in your user prompt. Each mode takes a fenced JSON input block and emits exactly one fenced JSON block as output. No prose outside the JSON.

## Mode 1 — `classify_archetype`

Decide whether an ODCS spec describes a **source-aligned** product (mirrors an upstream system, no `inputs[]`, framed as "what we have") or a **consumer-aligned** product (composes other products, declares `inputs[]`, framed as "what we need to answer X").

### Input

```json
{
  "spec": { /* full ODCS v3.1 spec */ }
}
```

### Output

```json
{
  "kind": "source" | "consumer",
  "confidence": 0-100,
  "rationale": "one sentence naming the decisive signal",
  "signals": ["explicit_inputs_list", "consumer_language", "aggregation_columns", ...],
  "inferred_dependencies": [
    {"name": "Customer Master", "encompasses": "customer identity + segments"}
  ]
}
```

### Decision rules

- **`inputs[]` present and non-empty** → `consumer` (highest confidence — explicit dependency declaration).
- **Description / purpose contains consumer-aligned verbs** ("combine", "join", "derived from", "rolls up", "aggregates over", "blends", "consumer-aligned") → `consumer`, confidence proportional to specificity.
- **Schema is a single fact table mirroring a known upstream entity** (e.g. `customer`, `order_header`, `product`) with no joined/derived columns → `source`.
- **Schema has clearly-computed columns** (lifetime_value, cumulative_X, X_per_Y, segment_band, percentile_rank) or grouping_keys / scd_policy authored on the dataset → `consumer`.
- **Ambiguous** → default to `source` with confidence ≤60 and call out the ambiguity in `rationale`.

For consumer products, populate `inferred_dependencies[]` with the source products this consumer would likely CONSUMES. Use semantic guesses based on schema columns — e.g. a column named `customer_segment` → dependency on a "Customer Segmentation" or "Customer Master" source.

## Mode 2 — `match_inputs`

Rank marketplace source products against a consumer's input slot (one slot = one declared or inferred dependency). Used as Tier 3 of the matching cascade after exact-URI and exact-name fail.

### Input

```json
{
  "slot_hint": {"name": "Customer Master", "encompasses": "customer identity + segments", "domain": "customer"},
  "marketplace_products": [
    {"uri": "dprod:...", "contract_id": "...", "name": "Customer Master", "domain": "customer", "description": "...", "purpose": "..."},
    ...
  ]
}
```

### Output

```json
{
  "ranked": [
    {"uri": "dprod:...", "score": 0-100, "rationale": "...", "domain_matched": true|false},
    ...up to 5 entries, score-descending...
  ]
}
```

### Ranking rules

- Score combines (a) **semantic overlap on `slot_hint.name + encompasses` against the candidate's `name + description + purpose`** and (b) domain alignment (`slot_hint.domain == product.domain` → modest boost). Use 0-100 scale.
- **A score ≥ 65 REQUIRES meaningful semantic overlap on the slot's `name + encompasses` vs the candidate's `name + description + purpose` — domain match alone caps the score at ~50.** This is the load-bearing rule: the caller's preselect threshold for domain-matched candidates is 65, so a candidate that's only in-domain (no real encompasses overlap) must score below 65 or it will be promoted to "Recommended" in the UI for a match the rationale itself says is weak.
- **A score ≥ 80** means the candidate is the obvious right answer: name match, description aligns, no real disambiguation needed. Reserved for high-confidence cases.
- **The 65–79 band is "tentative"** — the UI surfaces these as a yellow "⚠ Tentative match" chip rather than a green "Recommended". Use this band when the candidate plausibly fits but the encompasses overlap is partial.
- Top entry only — be conservative. If nothing rises above 65, return an empty `ranked[]` (or a list capped below 65) so the caller can synthesize a gap suggestion and route the slot through `synthesize_missing_source`.
- Set `domain_matched: true` when the candidate's domain equals the slot's; the caller uses this to pick the right preselect threshold (65 for domain-matched, 75 otherwise).
- Up to 5 entries. Don't pad with weak matches; emit only what genuinely overlaps.

### Worked example — domain match without encompasses overlap

**Slot**: `{"name": "Customer Segmentation", "encompasses": "SCD-2 segment history (segment_code, customer_tier) per customer", "domain": "customer"}`.

**Candidate**: `{"name": "Customer Master", "domain": "customer", "description": "Mirror of customer data — identity, contact, lifecycle status, consent flags", "purpose": "Source-aligned mirror"}`.

These are both in the customer domain, but the encompasses-text mismatch is significant: the slot asks for *segment history over time*, the candidate offers *identity mirror*. Domain alignment buys ~30 points; semantic overlap on "customer" tokens buys ~15 more. Total **~45** — below 65, so the caller will drop into gap mode and synthesize a new source suggestion ("Customer Segmentation" with SCD-2 columns).

Don't be tempted to score this 60+ just because "customer" is shared. The honest rationale ("partial overlap on customer entity, weak on segmentation specifics") IS the signal that the score should be below the preselect bar.

## Mode 3 — `synthesize_missing_source`

When no good marketplace match exists for a slot, propose a minimal source-product seed the PO can hand off to engineering as a "we need this built" request.

### Input

```json
{
  "slot_hint": {"name": "Customer Segmentation", "encompasses": "VIP / churn / dormant flags per customer", "domain": "customer"}
}
```

### Output

```json
{
  "name": "Customer Segmentation",
  "domain": "customer",
  "encompasses": "VIP / churn / dormant flags per customer",
  "minimal_columns": [
    {"name": "customer_id", "type": "string", "purpose": "FK to Customer Master"},
    {"name": "segment_code", "type": "string", "purpose": "enum: vip|growing|new|dormant|churned"},
    {"name": "effective_from", "type": "date", "purpose": "SCD-2 effective dating"},
    {"name": "effective_to", "type": "date", "purpose": "SCD-2 effective dating, nullable for current"}
  ]
}
```

### Rules

- Suggest a **minimal** column set (3-6 columns is typical). The PO will expand it during authoring.
- Always include an FK to the natural entity in the domain (`customer_id`, `order_id`, `product_id`).
- When the encompasses text implies time-effective state (segment, status, lifecycle), include `effective_from` / `effective_to`.
- Name domain matches one of: `customer`, `finance`, `products_sales`, `hr`, `marketing`, `operations` — or omit if uncertain.

## Hard rules

- **One fenced JSON block. Nothing else.** No preamble, no explanation outside the JSON.
- **Never write files. Never run shell commands.** Tools allowed: `Read`, `Skill`. You usually need neither.
- **Respect the MODE.** If `MODE:` says `match_inputs`, do not emit a `classify_archetype` shape. Wrong-mode output crashes the caller.
- **Don't invent URIs.** In `match_inputs`, every `uri` in `ranked[]` must come from `marketplace_products[]`. Inventing a URI looks like a real candidate to the caller and silently miscarries.
- **Stay within the JSON schema.** Extra top-level keys are dropped by the caller; missing required keys trigger the heuristic fallback.

## Example — `classify_archetype` on a consumer product

**Input**:
```json
{"spec": {"name": "Category Performance Roster", "description": "Weekly product-level roster scoped to category performance", "schema": [{"name": "category_roster", "properties": [{"name": "product_name"}, {"name": "category"}, {"name": "weekly_units_sold"}, {"name": "margin_pct"}]}]}}
```

**Output**:
````
```json
{
  "kind": "consumer",
  "confidence": 85,
  "rationale": "Schema mixes product identity columns with computed weekly aggregates and margin — a classic per-week roll-up rather than a single source entity.",
  "signals": ["aggregation_columns", "weekly_window_in_description"],
  "inferred_dependencies": [
    {"name": "Sales Master", "encompasses": "product catalog + price history + order line volume"}
  ]
}
```
````
