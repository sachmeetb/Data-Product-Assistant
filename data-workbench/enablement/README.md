# Data Workbench — Innovation-Team Enablement Track

A **new, hands-on enablement track** to ramp the Innovation team on Data Workbench: stand
the product up locally, build data products end-to-end, drive it over MCP, and understand
what's happening under the hood.

This is **distinct in purpose and tone** from the stage-zero / solutioning / positioning
material (outward, client-persuasion content). That material is *reference* here — draw
facts from it, never template it.

## Where things are

| File | What it is |
|---|---|
| [`enablement-plan.md`](./enablement-plan.md) | **The plan** (Phase 1) — module map, per-module scope, net-new register, verification. The canonical spec for the track. |
| [`ground-truth-facts.md`](./ground-truth-facts.md) | Locked fact sheet (ports, tool counts, skill count, `dwb` commands, stale-doc reconciliation). Anti-drift source for authoring. |
| [`activity-template.md`](./activity-template.md) | The reusable per-module template + tone contract + `[SCREENSHOT: …]` slot convention. |
| [`modules/`](./modules/) | **Phase 2** per-slide markdown, one file per module (`mNN-<slug>.md`). Authored — see index below. |

## Module index (Phase 2 — authored)

`m00-orientation.md` is the house-style exemplar; the rest match its format
(`## Slide N — Title` · `**Beat:**` · bullets · `> **Concept callout:**` · `[SCREENSHOT: …]`
slots · `_Speaker notes:_`). **160 slides across 11 modules**, within the 130–170 target.

| Module | File | Slides | Screenshot slots |
|---|---|---:|---:|
| M0 — Track Orientation & the Two-Persona Model | [`m00-orientation.md`](./modules/m00-orientation.md) | 10 | 2 |
| M1 — Windows → WSL2 Environment Prep | [`m01-wsl2-environment-prep.md`](./modules/m01-wsl2-environment-prep.md) | 14 | 4 |
| M2 — Install, Configure & Launch Locally | [`m02-install-configure-launch.md`](./modules/m02-install-configure-launch.md) | 17 | 5 |
| M3 — Flagship Pt 1: Two Source-Aligned Products | [`m03-flagship-source-products.md`](./modules/m03-flagship-source-products.md) | 22 | 11 |
| M4 — Flagship Pt 2: Aggregate & Lineage | [`m04-flagship-aggregate-lineage.md`](./modules/m04-flagship-aggregate-lineage.md) | 19 | 12 |
| M5 — Under the Hood: Skills, Graph, Transformations | [`m05-under-the-hood.md`](./modules/m05-under-the-hood.md) | 17 | 5 |
| M6 — Driving DW over MCP | [`m06-driving-over-mcp.md`](./modules/m06-driving-over-mcp.md) | 15 | 6 |
| M7 — Data Quality & Pipelines | [`m07-data-quality-pipelines.md`](./modules/m07-data-quality-pipelines.md) | 14 | 4 |
| M8 — Advisory Services for DE & PO | [`m08-advisory-services.md`](./modules/m08-advisory-services.md) | 11 | 3 |
| M9 — BYO Data → Consumer-Aligned Product | [`m09-byo-data-consumer-product.md`](./modules/m09-byo-data-consumer-product.md) | 14 | 8 |
| M10 — Feedback Loop, Roadmap & Placeholders | [`m10-feedback-roadmap.md`](./modules/m10-feedback-roadmap.md) | 7 | 0 |

**Inline markers to resolve before deck generation:**
- `[SCREENSHOT: …]` slots (**60 total**) are resolved during the flagship/MCP/independence
  rehearsals (plan verification gates 2–4) — that's where the shots come from.
- `[NET-NEW: …]` (M1/M2/M10) — content with no repo source, drafted here, to be confirmed
  with the DW team (hardware sizing, key rotation, secret hygiene, feedback template).
- `[VERIFY: …]` (5 items in M2/M3/M8) — small claims flagged for confirmation at rehearsal:
  CPU-core floor, disk headroom, concurrency framing, the exact quick-connect picker label,
  and whether the *interactive* DE Ask panel wires `get_plan_summary` (it exists as an MCP
  tool; the panel's system prompt doesn't reference it).

## The three phases

1. **Plan** *(done)* — module map + scope. This directory.
2. **Per-slide content** *(authored — in review)* — markdown per module (slide titles,
   bullets, concept callouts, `[SCREENSHOT: …]` slots) in [`modules/`](./modules/). Review
   each deck's story + screenshot list until agreed; then resolve the inline markers.
3. **Deck generation** — from approved markdown, tool TBD (acnpptx / python-pptx / other).
   Content stays **tool-agnostic** until then.

**We are not producing decks now** — Phase 2 is the per-slide markdown, not slides.

## The track at a glance

~11 decks (M0 orientation + ten content decks), ≈130–170 slides, three hands-on projects
plus a guided setup lab. Three arcs: **Set up (M0–M2) → Do (M3–M4) → Understand & Extend
(M5–M10).** See [`enablement-plan.md`](./enablement-plan.md#module-map-the-10-deck-sequence)
for the full module map.

## Hard scope exclusion

The **semantic layer** (Steward Concepts → Discovery sequence, `:BusinessConcept`s) is **out
of this initial track** — its own future chapter. Consequence baked in: validation uses the
**self-contained per-product Q&A tab**, not the domain Semantic Q&A chat (which degrades to
Full-Context-only without the semantic layer).

## Open decisions (do not block Phase 1)

- **Canonical latest-version distribution location** — repo `enablement/` vs external space
  vs both. Carried as a placeholder on every deck (M0) until resolved.
- **Deck-production tool** — decided at the production phase.
- **Feedback-loop channel** — issue template vs form (M10).
