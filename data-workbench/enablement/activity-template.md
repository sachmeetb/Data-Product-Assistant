# Enablement Activity Template

The single template **every hands-on module** uses, applied consistently. Concept modules
(M0, M5, M8, M10) drop the Steps/Validation mechanics but keep Objectives, tone, and the
version placeholder. Copy this file per module in Phase 2 → `modules/mNN-<slug>.md`.

---

## Tone contract (applies to every module)

- **Procedural + explanatory ramp-up, first-person-doer voice** ("you run…", "you'll see…").
- **Not** the maturity-matrix / "menu not monolith" positioning voice of the solutioning
  decks. This track teaches a practitioner to *operate the product*, not persuades a client.
- Prefer showing the real screen/command over describing it. Every hands-on claim should be
  something the author actually did in the rehearsal (Verification gate 2–4 of the plan).

---

## Screenshot slot convention

Author content **tool-agnostic** — no deck tool is chosen yet. Mark every image with an
explicit slot the production phase resolves:

```
[SCREENSHOT: <short-id>]
Caption: <one line the audience reads>
Must show: <exactly what must be visible in the frame — the pane, the value, the state>
Taken at: <the step/command that produces this screen>
```

One slot per distinct screen state. If a step has no screen, say so — don't pad.

---

## Per-module skeleton

### Module header
- **Module #/title**
- **Type:** concepts | hands-on setup | hands-on project | concepts + demo | meta
- **~Slide count**
- **Latest-version pointer:** `[PLACEHOLDER: canonical-latest-version location — TBD]`
  *(carry this on every deck until the open decision is resolved — see the plan)*

### 1. Objectives
What the learner can **do** at the end (1–3 outcomes, verbs not topics).

### 2. Prerequisites / starting point
- Which prior module(s) must be complete.
- The exact starting state ("stack up via `./dwb status` green", "two source products in the
  Marketplace", etc.) — the reader should be able to confirm they're at the start line.

### 3. Steps (screenshot-driven)
Numbered, imperative, one action per step. For hands-on modules, thread the **persona
context** explicitly (`**PO** …` / `**Switch → DE** …`) so the learner never loses track of
which shell they're in. Attach `[SCREENSHOT: …]` slots inline where a screen changes.

### 4. Outcome — what "done" looks like
A concrete end-state description + a `[SCREENSHOT: …]` of the finished result.

### 5. Validation / confirmation
The check the learner runs to *prove* success (a command, a Marketplace filter, a Preview
tab returning rows, a UI state transition). Every hands-on module ends with a green check the
learner performs themselves. **Semantic-layer-free rule:** validate product understanding via
the **per-product Q&A tab**, never the domain Semantic Q&A chat (out of scope this track).

### 6. Next steps
One line pointing to the next module and what it unlocks.

---

## Concept-module variant (M0/M5/M8/M10)

Keep the header (incl. version placeholder), Objectives, and Tone. Replace Steps/Outcome/
Validation with:
- **Concept callouts** — the ideas, each with a one-line "why it matters to a practitioner".
- **Concrete referent** — tie back to something the learner already built in M3/M4 (concepts
  land better with a referent than in the abstract).
- **Self-check** — a short quiz or "explain it back" prompt instead of a command.
