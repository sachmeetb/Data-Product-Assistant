# Inbound-intake sample payloads

These are **example envelopes** for the external assessment tool to POST to Data Workbench at:

```
POST /api/intake/submit
Authorization: Bearer <WB_INTAKE_TOKENS credential>
Content-Type: application/json
```

They exist to show the external-tool team **what we can process** — deliberately spanning a range of
structure and completeness, because the whole point of the intake pipeline is tolerance of messy,
inconsistent input. An LLM parser normalizes whatever it's given into a confidence-graded *scaffold
blueprint*; a human then reviews/edits before anything is created.

## The envelope (all you have to produce)

| field | required | notes |
|---|---|---|
| `scenario` | **yes** | `"migration"` or `"modernization"`. (Auto-classification is a future add.) |
| `external_ref` | **yes** | Your stable id for this engagement/finding. Re-POSTing the same `external_ref` **updates** the same submission (idempotent) — safe to retry. |
| `content[]` | **yes (≥1)** | Heterogeneous parts. Each `{kind, title, media_type?, body}`. `kind` ∈ `text \| table \| odcs \| json`. `body` is a string (or JSON for `kind:"json"`). Put whatever you have here — prose, CSV, an ODCS contract, an app inventory. |
| `hints` | no | Optional structured nudges you already know: `source_platform`, `target_platform`, `domain`. |
| `metadata` | no | Free-form passthrough (preserved verbatim; e.g. your report id, confidence, timestamps). |

**You do NOT send `source_system`** — it is derived from your credential (provenance is trusted from
the token, never the body).

## What "more structure" buys you

More structure → higher parser confidence → fewer fields flagged for the reviewer to confirm.
- **Best:** an ODCS contract part (`kind:"odcs"`), or a table of datasets/columns/types.
- **Good:** a JSON inventory + prose.
- **Fine:** prose only — it still works; the reviewer just confirms/fills more.

Nothing is rejected for being sparse: unknowns come back as `confidence: "missing"` for a human to
fill, never guessed.

## The samples

| file | scenario | structure | what it exercises |
|---|---|---|---|
| `migration-structured.json` | migration | high | platforms in hints + a CSV dataset/column/type table + prose |
| `migration-freeform.json` | migration | low | prose only, no table, target platform unstated (→ a gap) |
| `modernization-portfolio.json` | modernization | high | prose + a JSON app inventory + an embedded ODCS product + explicit source→consumer relationships |
| `modernization-sparse.json` | modernization | low | thin prose describing a vague modernization goal |
