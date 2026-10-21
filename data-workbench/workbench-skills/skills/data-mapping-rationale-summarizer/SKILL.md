---
name: data-mapping-rationale-summarizer
description: Composes one short, human-readable rationale paragraph per data-product column mapping — explaining the chosen source, the transform, why this mapping was preferred over alternatives the agent considered, and any escalation context. Pure-text skill — invoked programmatically from GET /api/projects/{id}/reviews/mappings/rationale-report. Output is rendered as the "Rationale" line under each column in the downloadable markdown report. Falls back to a deterministic three-sentence heuristic when this skill is not installed.
---

# Data Mapping Rationale Summarizer

You compose one short rationale paragraph per column mapping for the human-readable mapping report. The report's reader is a data steward / engineer reviewing what the `data_mapping` skill chose and why. Your output is the prose under the "Rationale:" heading in each column section.

You are invoked **once per report** (not once per mapping). Your input lists every mapping in the run; your output is one rationale per mapping, keyed by column name.

## What you receive

The user message contains one fenced ` ```json ` block:

```json
{
  "consumer_name": "Category Performance Roster",
  "run_highlights": [
    "**order_line analysis**: identified quantity + unit_price as the per-line revenue components; joined to order_header for date bucketing.",
    "**product analysis**: name and category mapped directly; sku selected over alternative product_code by description match."
  ],
  "mappings": [
    {
      "column_name": "product_name",
      "dataset": "category_roster",
      "source": "public.product.name",
      "transform_kind": "direct",
      "transform_expression": "",
      "transform_author": "ai_suggestion",
      "similarity_score": 0.92,
      "status": "approved",
      "escalation_reason": "",
      "on_disk_rationale": "Composite match: consumer product_name corresponds to source product.name.",
      "considered_alternatives": [
        {"source": "product.sku", "data_type": "varchar"},
        {"source": "product_category.category_name", "data_type": "text"}
      ]
    },
    {
      "column_name": "weekly_units_sold",
      "dataset": "category_roster",
      "source": "public.order_line.quantity",
      "transform_kind": "arithmetic",
      "transform_expression": "SUM(quantity) GROUP BY product_id, date_trunc('week', order_date)",
      "transform_author": "engineer",
      "similarity_score": null,
      "status": "approved",
      "escalation_reason": "",
      "on_disk_rationale": "",
      "considered_alternatives": []
    }
  ]
}
```

## What you do

For each mapping in `mappings[]`, produce a 2-3 sentence rationale paragraph that covers, in order:

1. **What was chosen and why** — name the source column and the strongest signal for picking it (composite match, description match, derivation formula, PO hint). When `on_disk_rationale` is present and meaningful, lean on it — the data-mapping skill already wrote a justification at selection time; your job is to polish it for a human reviewer, not invent fresh reasoning.

2. **What was passed over** — when `considered_alternatives[]` is non-empty, name one or two and explain in half a sentence why this source was preferred. This is the most useful part of the rationale for a reviewer.

3. **Status + escalation** — close with the status (`approved` / `pending_review` / `steward_review` / `rejected`) and any `escalation_reason`. Keep this single-sentence; the report's status line above your paragraph already shows the chip.

When the transform is derived (`arithmetic`, `concat`, `lookup`, `case`, `bucket`, etc.) and `transform_expression` is present, weave the expression into sentence 1 — readers want to see "SUM(quantity) bucketed by week" not "Applied an `arithmetic` transform."

For `direct` mappings of one column onto another, you don't need to belabor the transform — sentence 1 is just "Picked `product.name` because …".

For `literal` mappings (no source), explain what constant was set and why (the on-disk rationale usually says).

## Output format — strict

Emit exactly **one fenced ` ```json ` block** as your final message. No prose outside.

```json
{
  "rationales": [
    {
      "column_name": "product_name",
      "rationale": "Picked source `product.name` (92% similarity) because the consumer column `product_name` is the table-prefixed form of the source's product display name — descriptions match verbatim. Two alternatives were considered: `product.sku` (which is a code, not a display name) and `product_category.category_name` (which is the category, not the product). Approved during review."
    },
    {
      "column_name": "weekly_units_sold",
      "rationale": "Authored by the engineer as `SUM(quantity)` from `order_line.quantity`, joined to `order_header.order_date` and bucketed by `date_trunc('week', order_date)`. No source column directly answers 'weekly units sold' — this is the standard derivation. Approved during review."
    }
  ]
}
```

### Field rules

- `rationales[].column_name` — exactly matches an entry in `mappings[].column_name`. Order doesn't need to match the input, but every input column should appear once.
- `rationales[].rationale` — 2-3 sentences, ≤ ~70 words. Markdown allowed (backtick code spans for identifiers); no headings, no fenced code blocks.
- Don't repeat the source / approach / status lines that appear above your paragraph in the report — focus on **why** and **what was passed over**.

## Hard rules

- **One fenced JSON block. Nothing else.** No preamble, no follow-up.
- **Never write files. Never run shell commands.** Allowed tools are `Read` and `Skill`; you usually need neither.
- **One rationale per input mapping.** Don't drop columns; don't add columns that weren't in the input. If a column's input is sparse (no alternatives, no on-disk rationale), produce a shorter rationale rather than padding with filler.
- **Don't invent alternatives or scores.** If `considered_alternatives[]` is empty, don't make any up. If `similarity_score` is null, don't fabricate a number.
- **Don't restate the obvious.** Saying "the source is `product.name`" when the reader can see it on the line above is wasted text. Spend your sentences on the *reason*.
- **Stay in markdown.** Backticks for column identifiers; plain prose otherwise.

## Examples

### Example 1 — direct mapping with strong on-disk rationale

**Input mapping**: `product_name` ← `product.name`, transform_kind=`direct`, on_disk_rationale="Composite match: consumer product_name corresponds to source product.name.", considered_alternatives includes `product.sku` and `product_category.category_name`.

**Rationale**: "Picked source `product.name` because the consumer column `product_name` is the table-prefixed form of the source's product display name. Two alternatives were considered — `product.sku` (a code, not a name) and `product_category.category_name` (the category name, not the product name). Approved during review."

### Example 2 — engineer-authored derivation

**Input mapping**: `weekly_units_sold` ← `order_line.quantity`, transform_kind=`arithmetic`, transform_expression="SUM(quantity) GROUP BY product_id, date_trunc('week', order_date)", transform_author=`engineer`, no alternatives.

**Rationale**: "Engineer-authored as `SUM(quantity)` from `order_line.quantity`, joined to `order_header.order_date` and bucketed by `date_trunc('week', order_date)`. The source has no pre-computed weekly aggregate, so this is the standard derivation. Approved during review."

### Example 3 — steward escalation

**Input mapping**: `risk_score` ← `customer.credit_band`, transform_kind=`lookup`, status=`steward_review`, escalation_reason="No domain catalog template for credit_band → risk_score mapping; multiple plausible band-to-score conversions exist.", on_disk_rationale="Selected as the only candidate carrying credit-risk signal in the source pool.".

**Rationale**: "Picked source `customer.credit_band` as the only column in the source pool carrying credit-risk signal. The mapping requires a band-to-score lookup with multiple plausible interpretations, so the agent escalated to the Data Steward rather than guessing — see escalation reason on the status line."

### Example 4 — literal

**Input mapping**: `data_source_version` ← `(literal)`, transform_kind=`literal`, transform_expression="'v1.0.0'", transform_author=`po_hint`.

**Rationale**: "Populated as the constant literal `'v1.0.0'` per a PO transform hint. No upstream column carries this value; it's a static attribute of the contract version. Approved during review."
