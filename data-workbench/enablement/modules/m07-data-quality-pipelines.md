# Module 7 — Data Quality & Pipelines: Independently-Executable Outputs

- **Type:** concepts + light hands-on
- **Target length:** ~14 slides
- **Prereq:** Module 4 (an aggregate + its serving package) and Module 5 (skills / graph / transforms)
- **Latest-version pointer:** `[PLACEHOLDER: canonical-latest-version location — TBD]`
  *(carry on every deck until the open decision is resolved)*

> **Slide creator brief — Module 7 of 11.** House style is in **Module 0** — follow that
> format for every slide. The learner completed the flagship (Modules 3–4) and understands
> the graph model (Module 5). This module covers two related ideas: (1) the **DQ pipeline
> lifecycle** (Configure → Build → Run → Failure Analysis) for testing data quality against
> contract rules; and (2) the **"package IS the execution unit" doctrine** — every generated
> pipeline (DQ, serving view, materialized dbt, data migration, code migration) is a
> self-contained directory that runs with stdlib + database driver only, writes
> `run_result.json` (machine verdict) + `report.md` (human verdict), and exits non-zero on
> failure. The hands-on payoff: download a package, take the entire DW stack down, and run
> it — proving no DW runtime dependency.
> **Screenshot placeholders:** insert a **gray placeholder box** at the stated size —
> **Large** = full-width, ~50–60% of slide height (primary visual) · **Medium** = ~half the
> slide, shared with text · **Small** = ~quarter-slide, accent or confirmation shot.
> Do not omit placeholder boxes — they mark where live screenshots are inserted later.

> **House format note.** This module follows the Module 0 exemplar shape: every slide is
> `## Slide N — Title`, opens with a one-line **Beat**, then bullets; concept ideas get a
> `> **Concept callout:**`; images are `[SCREENSHOT PLACEHOLDER — SIZE: X]` slots with Name / Caption / Must show /
> Taken at; `_Speaker notes:_` is for the presenter, not the slide. This is a concept module
> with a short hands-on tail — the payoff is a package you run with the whole stack **down**.

---

## Slide 1 — DQ & Pipelines: outputs that outlive the tool
**Beat:** set the frame — the thing DW generates is a real, portable artifact, not a button inside DW.

- Two ideas ride together in this module: **Data Quality (DQ) testing** turns your contract's
  rules into runnable tests; and **every pipeline DW generates — DQ, serving, migration — is a
  self-contained package that runs on its own, with no DW at runtime.**
- The one claim you'll *prove with your own hands*: **download a package, take the whole stack
  down, run it, read its verdict.** If it runs with DW off, DW is genuinely not a runtime
  dependency.
- By the end you can explain the DQ lifecycle, the catalog-vs-dprod distinction, the
  "package is the execution unit" doctrine, and the two ways a client adapts these outputs.

> **Concept callout — why a practitioner cares.** A client's data platform team will run these
> pipelines in *their* CI, on *their* schedule, against *their* database. "No DW at runtime"
> is what makes that possible — and it's the difference between a demo and a deliverable.

_Speaker notes:_ This is the module where the "governed by construction, portable by design"
promise from Module 0 Slide 6 gets cashed in. Keep the tone matter-of-fact; the proof is the terminal.

---

## Slide 2 — Objectives & starting point
**Beat:** what you'll be able to do, and where you must be to start.

- **Objectives — after this module you can:**
  1. Walk the DQ lifecycle **Configure → Build → Run → Failure Analysis** and say what each step
     touches.
  2. Explain **catalog-mode vs dprod-mode** — the one switch that flips what gets tested.
  3. State the **"package IS the execution unit"** doctrine and why it means no runtime DW.
  4. Name the **two adaptation paths** (client edits the output; DW is configured to match client
     conventions).
- **Prerequisites / starting point:**
  - Stack came up cleanly in Module 2 (`./dwb status` green) — though for the hands-on tail you'll
    deliberately take it **down**.
  - From Module 3/Module 4 you have at least one deployed product with a **serving package** (a source
    product's view package is enough). A DQ package is even better if you added DQ testing.

> **Concept callout — you don't need a fancy product to feel this.** Any completed **Build** in
> your projects produced a downloadable package. That's your specimen for the hands-on tail.

---

## Slide 3 — The DQ mental model: rules → tests → run → report
**Beat:** the one-paragraph model everything else hangs off.

- Three things come together: **Rules** (statements of what "good" looks like, already living in
  the knowledge graph), **Tests** (runnable code generated *from* the approved rules — one check
  per rule), and **a Target** (the actual data the tests run against).
- DQ testing is the machine that goes **rules → tests → run against target → pass/fail report +
  the offending values.**
- You don't author tests by hand and you don't author rules here — the rules were approved
  upstream (PO validation gate in Module 3, or the contract wizard in Module 4). DQ testing just *compiles
  and runs* them.

> **Concept callout — one rule, one check.** Every generated expectation traces back to exactly
> one approved `:PropertyShape` rule on the graph. That traceability is why a failure tells you
> *which rule* broke and *which rows* broke it — not just "something's wrong".

> **Concept callout — DQ rules are candidates, not production rules — the review gate
> exists for a reason.** The AI-generated rules approved in the PO validation gate (Module 3) or
> Rule Coach wizard step (Module 4) are *proposals*, not final truths. The generated GX tests are
> exactly as good as the rules that were approved upstream. Approving all rules uncritically
> produces a green DQ badge that doesn't reflect real domain knowledge — the rules must be
> reviewed by someone who understands what "good data" means for this domain. The review gate
> is the product's way of making that responsibility explicit: a human decision, not a
> default.

---

## Slide 4 — The lifecycle: Configure → Build → Run → Failure Analysis
**Beat:** the four steps, and the fact that they mirror the serving lifecycle you already know.

- **Configure DQ** — pick the test **framework** (a tiny dialog; changeable later via
  *Reconfigure*). Two choices: **Great Expectations (GX)** — the rich expectation-suite library;
  or **Pure Python / Pandera** — lightweight schema validators, fewer deps, simpler CI.
- **Build DQ Package** — generates a **self-contained, runnable test package** from the approved
  rules.
- **Run DQ Tests** — actually executes the package against the target, records pass/fail counts +
  offending values, and loads results back into the graph.
- **DQ Failure Analysis** *(optional)* — an AI pass that writes a human-readable report grouping
  failures by table/column with the most common bad values.
- It mirrors serving's **Configure Serving → Build → Deploy**, so it should feel familiar.

[SCREENSHOT PLACEHOLDER — SIZE: Large]
Name: `dq-configure-build-run`
Caption: The DQ workflow on the pipeline board — Configure, Build, Run.
Must show: A project's pipeline board with the DQ stages visible (Configure DQ → Build DQ
Package → Run DQ Tests, ideally with DQ Failure Analysis), at least one showing **complete** so
the download control is visible.
Taken at: On a project that has run at least Configure + Build DQ.

---

## Slide 5 — Build needs no live DB (this is the crux)
**Beat:** the load-bearing fact — the package exists the moment Build finishes, before anything touches data.

- **Build generates code, not results.** Nothing runs against live data during Build — so the
  package appears **immediately**, and you can **⤓ Download** or **↑ Push to Git** the *moment*
  Build finishes.
- This is the same split the serving modes use since the "Configure → Build → Deploy"
  unification: **Build** assembles the runnable, downloadable, git-pushable package with **no
  live-DB dependency**; **Run/Deploy** is a separate step that executes it against the real target.
- Practical upshot: the package is **no longer trapped behind a failing deploy**. A broken
  database connection can't stop you from getting the artifact.

> **Concept callout — Build vs Run is the whole trick.** Because Build is DB-free, the artifact
> is decoupled from execution. That decoupling is *exactly* what lets the client run it later,
> elsewhere, with no DW in sight.

---

## Slide 6 — catalog-mode vs dprod-mode: the one switch
**Beat:** the same DQ machinery tests two different things — and one switch decides which.

- **catalog mode** — rules live on raw `:Column` nodes (observation rules, mined from profiling);
  tests run against the **source database**. This is the "test a raw dataset / a source
  pre-check" behaviour.
- **dprod mode** — rules live on `:DProdColumn` nodes (spec / domain / user rules, from the
  contract); tests run against the **deployed product's views** (`vw_*`). This is the "test the
  published product" behaviour.
- The mode is chosen **automatically by which workflow the stage runs in** (`product_dq_testing`
  → dprod; everything else → catalog). It mirrors the mapping engine's `--source-mode`.
- **Source-aligned and consumer-aligned products converge here:** both test the *deployed
  product against its contract's rules*; they differ only in how the rules got onto the
  contract (SA derives them from the source; CF authors them directly).

> **Concept callout — the archetype shapes the menu.** A consumer (`dpe-cf`) product has no raw
> `:Column` catalog, so DW only offers it **product** (dprod) testing. A source-aligned
> (`dpe-sa`) product sees both — a product suite plus an optional source pre-check.

---

## Slide 7 — The doctrine: THE PACKAGE IS THE EXECUTION UNIT
**Beat:** the single most important idea in the module — how DW actually "runs" anything.

- **DW does not deploy or serve through a separate internal path.** It runs the package's **own
  entry-point runner as a subprocess** and reads the structured `run_result.json` the runner
  writes. *The same package is what you download and run yourself.*
- The runners (`workbench/backend/serving_runners/`) are **stdlib + database-driver only — no
  `workbench.*` imports**, copied verbatim into each package. They know nothing about DW.
- Every run writes its own verdict:
  - **`run_result.json`** — machine/CI verdict (overall `status`, per-step results, `metrics`,
    timing). The **same v1 contract** serving *and* DQ packages emit, so one CI tool understands
    all of them.
  - **`report.md`** — the human-readable pass/fail report with sample offending values.
  - a framework-native results log per run (history).
- The process **exits non-zero on any failure**, so it drops straight into a **CI gate**. No graph
  connection is made — the package "reports its own verdict" far away from DW.

[SCREENSHOT PLACEHOLDER — SIZE: Medium]
Name: `package-file-tree`
Caption: A downloaded package's file tree — runner, requirements, README, no DW imports.
Must show: An unzipped package directory listing: the entry-point runner (`run.py` for a serving
package, or `run_gx_validations.py` / `run_all.py` for a DQ package), `requirements.txt`,
`README.md`, the helper(s) like `_wb_runresult.py`, and the core artifact (`view.sql` /
`migration.json` / `expectations/` / `validators/`).
Taken at: After ⤓ Download package on a completed Build, unzipped in a terminal or file explorer.

> **Concept callout — entry-point names differ, verdict doesn't.** Serving packages run via
> `run.py`; DQ packages run via `run_gx_validations.py` (GX) or `run_all.py` (Pandera). All of
> them write the **same** `run_result.json` shape — that's the portable contract.

---

## Slide 8 — One doctrine, many pipelines (DQ *and* modernization)
**Beat:** this isn't a DQ-only trick — the same package model powers migration and code migration.

- The `serving_package.py` assembler builds the same shape for every output kind:
  - **virtual view** (`CREATE OR REPLACE VIEW` DDL + `run_deploy.py`),
  - **dbt-materialized** (a real dbt project + `run_dbt.py` wrapping `dbt build`),
  - **lakehouse** (Parquet + DuckDB via `run_lakehouse.py`),
  - **data migration** (`dmig`) — a self-contained **DLT** lift-and-shift pipeline
    (`run_migration.py`),
  - **code migration** (`cmig`) — an `old/` → `new/` conversion package with its reviewed spec.
- All of them ride the **same `run_result.json` v1 contract** as the DQ packages. Modernization
  (`dmig`/`cmig`) and DQ are the same delivery idea wearing different clothes.

> **Concept callout — "pipelines serve both".** When someone asks whether DW is a quality tool
> or a migration tool, the honest answer is: it's a **pipeline generator**, and DQ testing +
> modernization are two families of pipeline that share one execution + verdict model.

> **Concept callout — serving mode decision rule.** If the data is mostly clean, serve via a
> **virtual view** — a pass-through to the source, no separate copy. If the data is very
> dirty and needs cleaning, serve via a **materialized table** — a separate database copy you
> own and clean, with an ETL job and an SLA for freshness. The clean/dirty assessment drives
> the mode; the SCD policy (SCD1 = latest state → view; SCD2 = accumulating history → must
> materialize) provides the mechanical test when "mostly clean" isn't enough to decide.

_Speaker notes:_ Migration/`cmig` are named-but-not-built-here — this is a *concept* slide.
Don't demo a migration; just show that the package family is uniform. (Module 10 lists dmig/cmig as
future chapters.)

---

## Slide 9 — Adaptation path (a): the client adapts the generated output
**Beat:** the first of two ways these outputs meet a client's reality.

- The package is a **real, portable directory of files** — not something locked inside DW. The
  client can open it, read the README (every package ships one, seven fixed sections), diff it,
  and **edit it** to fit their environment.
- Because what DW runs is **byte-identical** to what you download, an edit the client makes is an
  edit to the actual production artifact — there's no hidden second copy.
- Typical client edits: point `--conn-string` / `WB_TARGET_*` env at their own database, drop the
  runner into their CI, tweak the generated SQL, add their own scheduling wrapper.

> **Concept callout — "the repo holds artifacts, not data."** Push-to-Git commits the *code*
> (SQL, dbt project, runner, README, ODCS spec) — never Parquet/DuckDB binaries. The client
> versions and reviews the pipeline like any other code.

> **Concept callout — auto-generated README + "not just that the script runs."** Every
> package ships a README auto-generated by a purpose-built agent skill: environment
> variables, architecture, run instructions, in a fixed seven-section format. The platform
> goes further: it actually **executes** the generated code from within the workbench
> (the `run.py` / `run_gx_validations.py` subprocess model in Slide 7) and then verifies
> the data **lands correctly at the target** — not just that the script exits cleanly.
> `run_result.json` is the machine record of that verification; `report.md` is the
> human-readable verdict. The distinction matters: execution success and correctness
> verification are two different things, and the package reports both.

---

## Slide 10 — Adaptation path (b): DW is configured/customized to match the client
**Beat:** the second way — instead of the client bending the output, you bend DW to emit their conventions.

- This is **configuration/customization of DW itself**, so the *generated* output already matches
  house style. Four concrete levers:
  - **Dialect picker** — the `serving_virtual_view` stage has a `dialect` config field; the
    view-DDL compiler selects `PostgresDialect` / `SnowflakeDialect` / `DatabricksDialect` /
    `BigQueryDialect` / `MySQLDialect` (or ANSI) for casts, hashing, and REGEXP emission. "One
    recipe, different stove."
  - **`MaterializationTarget`** — a per-product dbt target connection, so materialized builds
    land in *their* warehouse.
  - **Git provider / naming** — Gitea or GitHub, one repo per product named after the
    `project_code`, configured in Settings → Git Integration.
  - **Fork + rebuild the capability artifact** — the transform-portability capability file
    (`workbench/backend/platform/transform_capabilities.v1.json`, owned by the
    `data-transform-translation` skill) is what validates which transforms are portable to which
    platform; a client with a bespoke platform forks and rebuilds it.

> **Concept callout — configuration, not a code fork.** Every lever here is a *setting or a
> regenerated artifact*, not a patch to DW's engine. That's the design intent: adapt by
> configuration, keep the core generic.

---

## Slide 11 — What underpins both paths: templates & archetypes
**Beat:** why any of this customization is even coherent — it rests on templates and catalogs.

- **Project archetypes scaffold workflow groups** (`archetypes.py`): choosing `dpe-sa` / `dpe-cf`
  / `dmig` / `cmig` etc. lays down the right ordered stages, so the *shape* of the pipeline is a
  template, not a bespoke build each time.
- **Curated catalogs live in `playbook/`** and feed the generators: `domain_catalogs/`
  (starter schemas + recommended rules), `odcs_templates/`, `transformation_catalogs/`,
  `scoring_rubrics/`, `discovery/`.
- Adapting DW to a client, then, is largely: pick the archetype, point the catalogs at the
  client's conventions, set the dialect/target/git — and the generated packages come out in
  house style.

> **Concept callout — the referent you already saw.** In Module 4 the aggregate defaulted to a
> *materialized* serving recommendation. That default came from an archetype/heuristic template,
> not a hard-coded rule — the same template machinery you'd tune for a client.

---

## Slide 12 — Hands-on: run a package with the stack DOWN
**Beat:** the proof — you personally execute a generated package with DW switched off.

- **Steps:**
  1. On any completed **Build** (a serving Build from Module 3/Module 4, or a DQ Build), click **⤓ Download
     package** and unzip it somewhere outside the repo.
  2. Take the whole stack down: `./dwb down`. (Confirm it's really off — `./dwb status` should no
     longer be green; `curl :8000/api/health` should fail.)
  3. In the unzipped package: `python -m venv .venv && . .venv/bin/activate` then
     `pip install -r requirements.txt`.
  4. Run the entry-point runner against a reachable database:
     - **serving view package:** `cp .env.example .env`, edit target creds,
       `set -a && . ./.env && set +a`, then `python run.py --apply`.
     - **DQ package:** `python run_gx_validations.py --conn-string "postgresql://user:pass@host:5432/db"`
       (GX) or `python run_all.py --conn-string "…"` (Pandera).
  5. Watch it run to completion — **with DW still down.**

[SCREENSHOT PLACEHOLDER — SIZE: Large]
Name: `run-py-stack-down`
Caption: The package's runner executing in a terminal with the DW stack stopped.
Must show: Two things in one frame if possible — a `./dwb status` (or failing `curl
:8000/api/health`) proving the stack is down, and the package's runner mid-run / finishing in
the same shell.
Taken at: Step 4–5, immediately after `./dwb down`.

> **Concept callout — reachable ≠ DW.** The database the package talks to can be *any* reachable
> DB (even a cloud one). What it does **not** need is DW, its backend, or Neo4j. That's the
> whole point.

---

## Slide 13 — Validation: read the verdict the package wrote itself
**Beat:** the green check — the package reported its own result, no graph, no DW.

- **Confirmation to run:**
  - Open **`run_result.json`** — confirm it has `contract_version`, an overall `status`
    (`success`/`failed`), per-step results, and `metrics`. This is the machine verdict a CI job
    would parse.
  - Open **`report.md`** — the same story for humans: overall PASS/FAIL, a per-table table, and
    any failed check with its sample offending values.
  - Confirm the process **exit code** matched the verdict (non-zero on failure): `echo $?`.
- If all three line up **while DW is down**, you've proven the "no runtime dependency" claim
  first-hand.

[SCREENSHOT PLACEHOLDER — SIZE: Large]
Name: `run-result-json`
Caption: The `run_result.json` + `report.md` a package emitted on its own.
Must show: `run_result.json` open showing `status` + `metrics`, ideally beside `report.md`'s
PASS/FAIL summary. The stack is still down.
Taken at: After the run in Slide 12 completes.

> **Concept callout — one contract, every pipeline.** The `run_result.json` you're reading is
> the *same shape* whether this was a DQ, serving, or migration package. Learn to read it once;
> you can grade any DW pipeline's output.

_Speaker notes:_ If the learner's run failed on a missing driver/dialect, that's expected and
legible — DQ product testing preflights the SQLAlchemy dialect and prints an actionable
`pip install …`. A failed *run* still writes a verdict; that's a valid demonstration too.

---

## Slide 14 — Self-check + next steps
**Beat:** close the module; confirm the ideas landed; hand off to Module 8.

- **Self-check (answer before moving on):**
  1. During **Build**, does anything touch the live database? Why does the answer matter?
  2. What single thing decides **catalog-mode vs dprod-mode**, and what does each test against?
  3. What two files does a package write on every run, and which one is for CI?
  4. Name the **two adaptation paths** and give one concrete lever for path (b).
  5. Why can a DQ package and a serving package be parsed by the *same* CI tooling?
- **Latest version lives at:** `[PLACEHOLDER: canonical-latest-version location — TBD]`.
- **Next:** **Module 8 — Advisory Services for DE & PO.** You've seen how DW's *outputs* stand alone;
  next you'll use the *coaching surfaces* that help a DE and a PO produce them — and learn
  exactly where those surfaces' scope ends.

_Speaker notes:_ If a learner can't answer #1 or #2, replay Slides 5–6 before Module 8 — those two are
the module's load-bearing concepts.
