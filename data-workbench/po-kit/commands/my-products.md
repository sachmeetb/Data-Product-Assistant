---
description: List your data products and where each one stands (draft / in engineering / live)
argument-hint:
allowed-tools: mcp__workbench-po__get_po_summary, mcp__workbench-po__list_my_products, mcp__workbench-po__list_marketplace
---

The user wants an at-a-glance view of **their** data products — what they own and
where each one is in its lifecycle.

Do this:

1. Call `mcp__workbench-po__get_po_summary` for the headline picture (what they
   own, counts) and `mcp__workbench-po__list_my_products` for the per-product
   list.
2. Present a **concise, business-friendly** summary — not a data dump:
   - A one-line header (how many products, how many live).
   - Each product as `<name> — <status>` with a clear glyph
     (✅ published/live · 🟡 in engineering / awaiting review · ✏️ draft · ❌ rejected).
     Group by status if there are many.
   - For anything **waiting on the owner** (a validation to approve, a pushback,
     a source-candidate request), call it out — that's where they can act now.
3. If a product is live, offer its marketplace link (from the tool's `web_url`).
4. End by offering the obvious next move: open a product's detail, start a new
   one, or clear a pending decision.

Read-only — don't create, submit, or deploy anything here. Keep it scannable and
in plain business language (no tool names, no column-level detail).
