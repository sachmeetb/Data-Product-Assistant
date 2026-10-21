# Module 10 — Feedback Loop, Roadmap & Placeholders

- **Type:** meta (capstone — no hands-on)
- **Target length:** ~7 slides
- **Prereq:** none (capstone; best taken right after Module 9 while a fresh build is in mind)
- **Latest-version pointer:** `[PLACEHOLDER: canonical-latest-version location — TBD]`
  *(carry on every deck until the open decision is resolved)*

> **Slide creator brief — Module 10 of 11.** House style is in **Module 0** — follow that
> format for every slide. This is the **capstone meta-module — no hands-on build, no
> screenshots**. It is best taken right after Module 9 while the build is fresh. The module
> closes the track by: (1) collecting practitioner feedback via a structured template and a
> to-be-defined channel; (2) being honest about what is deferred (the semantic layer is the
> biggest: out of scope for this track and its own future chapter); and (3) naming genuine
> product gaps (auth/RBAC is frontend-only today). The "now vs coming" table on Slide 6 is
> the track's most useful single-page summary. There are no screenshot slots in this module.

---

## Slide 1 — You built it; now help shape what it becomes
**Beat:** set the frame — this module closes the loop, it doesn't teach a new build.

- Across Module 0–Module 9 you stood DW up, built source / aggregate / consumer products, drove it over MCP,
  and brought your own data through to a live product.
- This capstone does two things: **(1)** gives you a **channel to feed your experience back** to
  the DW team, and **(2)** is **honest about what's deferred** so you know the edges of the map.
- No hands-on here — a **reflection / self-check** instead.

_Speaker notes:_ Keep this short and candid. The Innovation team is the DW team's fastest signal;
the goal is to make that signal easy to send and to set accurate expectations.

---

## Slide 2 — Why your feedback is the roadmap
**Beat:** the Innovation team's experience *is* the highest-signal input.

- You just operated the product end-to-end. **Every point of friction you hit is data** the DW
  team can't get from a spec review.
- The most valuable notes are the **specific, reproducible** ones: "on Module 9's serving step, X
  happened when I expected Y." Small, concrete, traceable — not "the wizard felt clunky."
- **Change proposals are welcome too** — you've seen enough of the mechanics (skills, the graph,
  transforms) to suggest *how*, not just *what*.

> **Concept callout — feedback beats folklore.** A note filed against a module + a screen state
> becomes a tracked item. A hallway comment evaporates. The channel below exists to make the
> first path the easy one.

---

## Slide 3 — The feedback channel
**Beat:** where feedback goes and in what shape.

- `[NET-NEW: define the channel with the DW team — no existing doc.]` **Recommended first cut:**
  a **structured issue** in the DW repository's tracker. The stack already ships a git provider —
  **Gitea at `:3101`** (on by default) — so an in-repo **issue template** is the lowest-friction
  option; a lightweight **intake form** is the alternative if issues aren't the team's norm.
- **Decision to lock:** *issue template vs form* (open decision carried from the plan). Pick one
  and publish it alongside the canonical latest-version location.
- Until it's locked, use the **template on the next slide** so nothing gets lost in the meantime.

_Speaker notes:_ Don't over-engineer this. One agreed template + one known destination beats a
perfect process nobody follows.

---

## Slide 4 — A first-cut feedback template
**Beat:** something concrete the presenter can hand out today.

- `[NET-NEW: draft template — refine with the DW team.]` Copy/paste per report:

```
Title:        <module> — <one-line summary>

Context:      Which module/step, what you were trying to do.
              (e.g. "Module 9, Configure Serving on my consumer product")

What I tried: The exact actions / commands / clicks.

Expected:     What you thought would happen.

Actual:       What actually happened (paste output; screenshot the SCREEN — never the .env).

Proposal:     Your suggested change or question (optional but valued).

Environment:  DW version/commit, deployment (compose vs host), OS/WSL, LLM path
              (Foundry vs direct-Anthropic).
```

- Two habits to bake in: **reproduction steps** over adjectives, and **secret hygiene** — screenshot
  the screen, never the `.env` (it holds a live-looking key).

---

## Slide 5 — What's deferred (named here, chapters later)
**Beat:** be honest about the edges of this track — and of the product.

- **The big one — the Semantic Layer** (Steward Concepts / business concepts, the Discovery
  sequence, `:BusinessConcept`s): **out of scope for this track, its own future chapter.** It's
  *why* we validated on each product's **own Q&A tab** all track long, never the domain Semantic
  Q&A chat.
- **Deferred from this track (already in the product), future enablement chapters:**
  - **Advanced serving / lakehouse / object-store** — materialized dbt paths, Parquet/lakehouse;
    the SeaweedFS object store is **opt-in and off by default** (`--with storage`).
  - **Migration** — `dmig` (data lift-and-shift) and `cmig` (code migration).
  - **Connected-Estate feasibility** — the top-down "grade a catalog of desired products"
    surface.
- **A genuine product gap, not just a track gap — auth / RBAC hardening.** Today there is **no
  auth middleware**; persona access is **enforced in the frontend only**. Hardening this is
  roadmap, not shipped.

> **Concept callout — "named but not built" cuts both ways.** Some items above (migration,
> feasibility) *are* built in the product but **not yet in this enablement track**; others (RBAC
> hardening, the fuller semantic layer) are genuine roadmap. Say which is which — don't imply a
> gap is shipped or a shipped feature is missing.

---

## Slide 6 — What you can do now vs what's coming
**Beat:** recap against the Module 0 track outcomes.

| You can do this **now** (this track) | Coming / deferred |
|---|---|
| Run DW locally — healthy stack, samples connected (Module 1–Module 2) | Advanced serving: lakehouse / object-store |
| Build source, aggregate, and consumer products with lineage (Module 3–Module 4, Module 9) | The **semantic layer** (the headline future chapter) |
| Explain skills, the knowledge graph, transformations (Module 5) | Migration: `dmig` (data) + `cmig` (code) |
| Drive DW over MCP — **141** DE tools / **57** PO tools, **69** skills (Module 6) | Connected-Estate feasibility |
| Build & run independently-executable DQ/serving packages (Module 7) | Auth / RBAC hardening (today: frontend-only access) |
| Use the DE "Ask" + PO "Guide me" advisory surfaces (Module 8) | *(your feedback shapes what lands next)* |

_Speaker notes:_ Numbers move — re-verify tool/skill counts against `ground-truth-facts.md` at
authoring time (141 / 57 / 69 as of 2026-08-25).

---

## Slide 7 — Reflection + where to get the latest
**Beat:** close the track; confirm the mental model; point at the canonical source.

- **Explain-it-back (no peeking):**
  1. Which shell does the **PO** work in, which does the **DE** work in — and why the split?
  2. Name the three `productKind` values; which one "consumes" others?
  3. Why does **lineage** just work? *(hint: the graph is the source of truth)*
  4. Which validation surface did we use all track — and which did we avoid, and why?
  5. Name one thing you'd change, and file it using Slide 4's template.
- **Latest version lives at:** `[PLACEHOLDER: canonical-latest-version location — TBD]`
  *(open decision — every deck carries this until it's set)*.
- **Next:** there is no next module — **the feedback loop begins.** File your first note, and
  watch the canonical location above for new chapters (the semantic layer first).

_Speaker notes:_ If a learner stumbles on #1–#3, they should replay Module 0 (Slides 4–5) and Module 5
before considering the track complete.
