# Data Quality Testing — User Guide

> **In one sentence:** Data Quality (DQ) testing turns the quality *rules* attached to
> your data into runnable *tests*, executes them against real data, and gives you a
> pass/fail report — so you can prove the data is what the contract promises.

This guide explains **what** DQ testing does, **how** to use it, and — importantly —
**how it behaves differently** depending on the kind of thing you're testing. There's a
short [Deep dive](#deep-dive-under-the-hood) at the end for the technically curious; you
don't need it to use the feature.

---

## 1. The mental model

Three things come together:

1. **Rules** — statements about what "good" looks like ("`email` must not be null",
   "`amount` is between 0 and 10,000", "`status` is one of {active, closed}"). Rules
   already live in the knowledge graph before you test — you don't write them here.
2. **Tests** — runnable code generated *from* the approved rules. Each rule becomes one
   check.
3. **A target** — the actual data the tests run against.

DQ testing is the machine that goes **rules → tests → run against target → report**.

```mermaid
flowchart LR
    R["Approved quality rules<br/>(in the knowledge graph)"] --> B["Build a test package<br/>(one check per rule)"]
    B --> X["Run against the target data"]
    X --> Rep["Pass / fail report<br/>+ unexpected values"]
    style R fill:#eef2ff,stroke:#6366f1
    style Rep fill:#f0fdf4,stroke:#16a34a
```

The **only** thing that really changes between scenarios is **where the rules come from**
and **what data the tests run against**. Everything else — the lifecycle, the frameworks,
the reports — is the same.

---

## 2. Which scenario am I in?

The Workbench has three kinds of project ("archetypes"), and DQ testing means something
slightly different in each.

| You're working on… | Archetype | What you're testing | Where the rules come from |
|---|---|---|---|
| **A raw dataset** (just discovered + profiled a database) | `dq` / `dd` | The **source tables themselves** | *Discovered* — mined from profiling the data, then you pick which to keep |
| **A source-aligned product** (a product that mirrors a source 1:1) | `dpe-sa` | The **published product** (its served views); optionally the raw source as a pre-check | *Discovered from the source*, then approved by the Product Owner and baked into the contract |
| **A consumer-aligned product** (a product built from other products) | `dpe-cf` | The **published consumer product** (its served views) | *Authored by the Product Owner* directly on the contract |

A quick way to remember it:

```mermaid
flowchart TD
    Q{What are you testing?}
    Q -->|Raw discovered tables| DS["**Dataset**<br/>test the source data<br/>rules from profiling"]
    Q -->|A product that mirrors a source| SA["**Source-aligned**<br/>test the product<br/>rules derived from source, PO-approved"]
    Q -->|A product built from other products| CF["**Consumer-aligned**<br/>test the product<br/>rules authored on the contract"]
    style DS fill:#fef9c3,stroke:#ca8a04
    style SA fill:#e0f2fe,stroke:#0284c7
    style CF fill:#f3e8ff,stroke:#9333ea
```

> **Key idea:** source-aligned and consumer-aligned **converge at test time** — both test
> the *deployed product* against the rules on its *contract*. They differ only in how the
> rules got onto the contract (source-aligned *derives* them from the source; consumer-aligned
> *authors* them directly).

---

## 3. The lifecycle: Configure → Build → Run

However you got your rules, running DQ tests is always the same three steps (plus an
optional fourth). It mirrors the serving lifecycle (Configure Serving → Build → Deploy),
so it should feel familiar.

```mermaid
flowchart LR
    C["**Configure DQ**<br/>pick the framework"] --> B["**Build DQ Package**<br/>generate runnable tests<br/>(downloadable + git-pushable)"]
    B --> R["**Run DQ Tests**<br/>execute against the target<br/>load results into the graph"]
    R -. optional .-> F["**DQ Failure Analysis**<br/>readable report of what failed"]
    style C fill:#eef2ff,stroke:#6366f1
    style B fill:#eef2ff,stroke:#6366f1
    style R fill:#f0fdf4,stroke:#16a34a
    style F fill:#fafafa,stroke:#a3a3a3
```

- **Configure DQ** — choose your test **framework** (see [§5](#5-choosing-a-framework)).
  A tiny dialog; you can change it later with *Reconfigure*.
- **Build DQ Package** — generates a **self-contained, runnable test package** from the
  approved rules. Nothing runs against live data yet — so the package appears immediately,
  and you can **download it** or **push it to git** the moment Build finishes.
- **Run DQ Tests** — actually executes the package against the target data, records pass/fail
  counts and the offending values, and loads the results back into the Workbench.
- **DQ Failure Analysis** *(optional)* — an AI pass that writes a human-readable report
  grouping failures by table and column with the most common bad values.

### How to do it (web UI)

1. On the project's pipeline board, add the right DQ workflow from **+ Add Workflow**:
   - **Dataset / source-aligned pre-check:** *DQ Testing* (for a `dq` project this also
     auto-adds *Baseline DQ Rules* so you have rules to test).
   - **A product (source- or consumer-aligned):** *Product DQ Testing*.
   - The catalog only offers what makes sense for your archetype — you won't see product
     testing on a raw dataset, or source testing on a consumer product.
2. *(Dataset / catalog only)* Run **DQ Rule Generation** to mine rules from profiling.
   (Products skip this — their rules are already on the contract.)
3. Run **Configure DQ** → pick a framework.
4. Run **Build DQ Package** → optionally **⤓ Download package** or **↑ Push to Git**.
5. Run **Run DQ Tests**.
6. *(optional)* Run **DQ Failure Analysis**.

### How to do it (from your own Claude Code / MCP)

The same steps map to MCP tools: `add_workflow` → *(catalog)* `run_stage` for rule
generation → `select_exclusive_group` to pick the framework + `complete_stage` for
`configure_dq` → `run_stage` for the build and the execution → `get_dq_rules` /
`get_dq_test_runs` / `get_dq_package` to read results and pull the package.

---

## 4. Walkthrough by scenario

### 4a. Dataset (`dq` / `dd`) — test the source data

You discovered and profiled a database and want to check its quality **in place**.

- **Rules:** *discovered.* The **DQ Rule Generation** step reads the profiling evidence
  (null rates, value distributions, uniqueness, ranges) and proposes **observation** rules.
  You (or the Data Quality Analyst) approve the ones worth keeping.
- **Target:** the **raw source tables** — the tests connect straight to the source database.
- **Use it when:** you're assessing a database's quality, independent of any product.

```mermaid
flowchart LR
    P["Profile the source"] --> G["Generate rules<br/>(observation)"] --> A["Approve rules"]
    A --> Tst["Build + run tests"] --> DB[("Source database<br/>raw tables")]
    style DB fill:#fef9c3,stroke:#ca8a04
```

### 4b. Source-aligned product (`dpe-sa`) — test the published product

The product mirrors a source system 1:1. The engineer profiles the source, the Product
Owner validates the names/descriptions/rules, and the product is published as a set of
**views**.

- **Rules:** *derived, then approved.* Observation rules from profiling the source are
  reviewed by the PO and **materialized onto the product's data contract**.
- **Target (primary):** the **deployed product** — the served views, validated against the
  contract's rules. Add **Product DQ Testing**.
- **Target (optional pre-check):** you can *also* run catalog-mode **DQ Testing** against
  the raw source *before* materializing, as an early smoke test. Both are offered for
  source-aligned projects.

```mermaid
flowchart LR
    S["Source"] --> Pr["Profile → observation rules"]
    Pr --> PO["PO validates → rules on the contract"]
    PO --> Dep["Deploy product views"]
    Dep --> T["Product DQ Testing<br/>(test the views vs the contract)"]
    Pr -. optional early check .-> Pre["DQ Testing<br/>(test the raw source)"]
    style T fill:#e0f2fe,stroke:#0284c7
    style Pre fill:#fafafa,stroke:#a3a3a3
```

### 4c. Consumer-aligned product (`dpe-cf`) — test the published product

The product is assembled from **other products** (via the marketplace). There's no raw
database to profile — the quality expectations are written by the Product Owner as part of
the contract.

- **Rules:** *authored.* The PO writes rules on the contract during the product wizard
  (plus any inherited from domain catalogs or an ingested spec). There is **no rule
  generation step** — the rules already exist.
- **Target:** the **deployed consumer product** — its served views, validated against the
  contract's rules. Add **Product DQ Testing**.
- **Note:** catalog-mode DQ (source testing) is **hidden** for consumer products — there's
  no raw `:Column` catalog to test, so offering it would be misleading.

```mermaid
flowchart LR
    Src1["Source product A"] --> Prod["Consumer product<br/>(built via CONSUMES)"]
    Src2["Source product B"] --> Prod
    PO["PO authors rules on the contract"] --> Prod
    Prod --> Dep["Deploy product views"]
    Dep --> T["Product DQ Testing<br/>(test the views vs the contract)"]
    style T fill:#f3e8ff,stroke:#9333ea
```

---

## 5. Choosing a framework

**Configure DQ** lets you pick how the tests are written:

| Framework | What it is | Pick it when |
|---|---|---|
| **Great Expectations (GX)** | The industry-standard expectation-suite library | You want the richest expectation vocabulary / integration with a GX ecosystem |
| **Pure Python (Pandera)** | Lightweight schema validators in plain Python | You want fewer dependencies / a simpler package to run in CI |

You can switch frameworks at any time (*Reconfigure*); the next **Build** regenerates the
package in the chosen framework. Only one framework is active at a time.

---

## 6. The DQ package — download, push, and run it yourself

A big part of this feature is that the **test package is a real, portable artifact** — not
something locked inside the Workbench. After **Build DQ Package** completes you can:

- **⤓ Download package** — a zip with the test code, a `requirements.txt`, and a README.
- **↑ Push to Git** — commit it to the product's git repository alongside the serving
  package and docs.

### Running the package outside the Workbench

Install and run it against any reachable database:

```bash
pip install -r requirements.txt
# Great Expectations:
python run_gx_validations.py --conn-string "postgresql://user:pass@host:5432/db"
# Pandera:
python run_all.py            --conn-string "postgresql://user:pass@host:5432/db"
```

The process **exits non-zero if anything fails**, so it drops straight into a CI gate.

### What it reports (no Workbench required)

Every run writes three files into the output folder, so the package **reports its own
verdict** even when it's run far away from the Workbench (and with no knowledge-graph
connection):

| File | For | Contents |
|---|---|---|
| `run_result.json` | **machines / CI** | A standard verdict: overall `status` (success/failed), per-table results, and metrics (checks passed/failed, which tables failed). Same shape the serving packages emit. |
| `report.md` | **people** | A readable report: overall PASS/FAIL, a per-table table, and each failed check with its unexpected sample values. |
| `…_results_<timestamp>.json` | history | The full framework-native result log, one per run. |

Inside the Workbench, running the tests **also** loads the results into the knowledge graph
so the dashboards and the marketplace can show quality trends over time.

---

## 7. Reading the results

- **In the UI:** the **DQ Rules** card shows the rules being tested; run history and pass/fail
  come from the test runs. **DQ Failure Analysis** produces the most readable summary — start
  there when something fails.
- **The `report.md`** in the package is the same story in portable form.
- **Severity matters (Pandera):** a `sh:Violation` failure fails the run; a `sh:Warning`
  failure is reported but does *not* fail the run — so warnings surface issues without
  blocking a CI gate.

---

## 8. Gaps & outstanding items

Honest list of what's **not** done yet or only partially supported. None of these block the
common paths above.

| Item | Impact today | Status |
|---|---|---|
| **Source-aligned "both suites at once"** | A `dpe-sa` project can't yet hold a *source pre-check* suite **and** a *product* suite simultaneously — they'd write to the same folder and collide. Running one at a time is fine. | Deferred (needs separate output folders per mode) |
| **Product-test result → column linkage** | When product tests run, results are recorded, but individual failures don't yet link back to the specific product column node in the graph (they link cleanly for source tests). Reports are unaffected; some graph drill-downs are thinner. | Graceful degradation; loader enhancement pending |
| **Product testing platform support** | The Workbench now **executes** product tests against Postgres, **Databricks**, Snowflake, or MySQL (the generated GX runner is multi-platform). Each non-Postgres platform needs its SQLAlchemy dialect installed on the backend (`databricks-sqlalchemy` and `pymysql` ship by default; `snowflake-sqlalchemy` is optional); if a dialect is missing the run stops with an actionable `pip install …` message. An unrecognised served platform fails closed. | Postgres + Databricks/Snowflake/MySQL |
| **Rules must exist first for products** | Product testing does **not** generate rules — it tests the rules already on the contract. If a contract has few rules, coverage is thin. Rule *generation* is a source/dataset capability only. | By design |
| **Live validation of the product path** | The product-testing query logic is built against the same graph shapes the rest of the app uses, but hasn't been exercised end-to-end on a live deployed consumer product in this environment yet. | Verification pending |

---

## Deep dive (under the hood)

*You don't need this section to use DQ testing.* It's here for engineers who want to know
exactly how the two behaviors are implemented. The plain-English version: **the Workbench
keeps quality rules in a graph database, attached either to raw source columns or to product
columns; "which columns" is the one switch that flips everything between the two modes.**

### The one switch: `catalog` vs `dprod` mode

Under the hood there are exactly **two modes**, chosen automatically by *which workflow* the
DQ stages run in (`stage_execution.resolve_dq_source_mode` — `product_dq_testing` → `dprod`,
everything else → `catalog`). This mirrors the mapping engine's `--source-mode`.

```mermaid
flowchart TB
    subgraph catalog["catalog mode  (dataset + SA pre-check)"]
        C1["Rules on :Column<br/>(observation, from profiling)"] --> C2["Tests run vs the SOURCE database"]
    end
    subgraph dprod["dprod mode  (SA + CF products)"]
        D1["Rules on :DProdColumn<br/>(spec / domain / user, from the contract)"] --> D2["Tests run vs the DEPLOYED product views"]
    end
    style catalog fill:#fffbeb,stroke:#ca8a04
    style dprod fill:#faf5ff,stroke:#9333ea
```

- **catalog** is byte-for-byte the historic behavior: approved `:PropertyShape` rules
  reached via `(:Dataset)-[:HAS_SHAPE]->(:NodeShape)-[:PROPERTY]->(:PropertyShape)-[:ON_COLUMN]->(:Column)`,
  executed against `project.pg_connection`. Rule generation (observation-from-profiling)
  only exists here.
- **dprod** reads contract rules via
  `(dc:DataContract)-[:MATERIALISES_AS]->(dp:DProdDataProduct)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)-[:DPROD_OUTPUT_DATASET]->(:DProdOutputDataset)-[:HAS_PRODUCT_COLUMN]->(:DProdColumn)<-[:ON_DPROD_COLUMN]-(:PropertyShape)`,
  filtered to `ruleSource IN ['spec','domain','user']`. Tests are keyed to the deployed
  view name `vw_<safe_name(physicalName)>` (a helper kept byte-identical to the serving
  view generator), and executed against the **deployed product's** connection.

### Where rules come from (the four sources)

Every rule is a `:PropertyShape` tagged with a `ruleSource`:

| `ruleSource` | Origin | Anchored to | Seen in mode |
|---|---|---|---|
| `observation` | Mined from profiling | `:Column` | catalog |
| `domain` | A domain rule catalog | `:DProdColumn` | dprod |
| `user` | Authored by the PO in the wizard | `:DProdColumn` | dprod |
| `spec` | Embedded in an ingested ODCS contract | `:DProdColumn` | dprod |

Only **approved** rules generate tests (`coalesce(ps.status,'approved')='approved'`), which
keeps the test surface aligned with the marketplace and scoring.

### Connection resolution & guards (dprod)

For a product, "the target" isn't the source database — it's wherever the product's views
were deployed. The executor resolves it with the **same served-location-first resolver**
(`resolve_read_connection_for_consumer`), which transparently handles a source-aligned
product's own connection *and* a consumer-aligned product's connection **borrowed via
`:CONSUMES`** — reading from where the upstream source was actually *materialized* (e.g. a
MySQL source loaded into Databricks), not its origin. Two pre-flight guards keep failures
legible rather than silent:

1. **Dialect preflight** — Postgres runs against its DSN directly; a Databricks/Snowflake/MySQL
   served target builds a DSN via `build_connection_string` and is gated on its SQLAlchemy
   dialect being installed (a missing driver returns an actionable `pip install …` message,
   an unrecognised platform fails closed) — instead of a broken run.
2. **Deploy-first** — the product's virtual view must be deployed
   (`:ServingDefinition.deploymentStatus = 'deployed'`), because the tests reference the
   `vw_<name>` relations the deploy created.

### The `run_result.json` contract

The `run_result.json` a DQ package writes is the **same v1 contract** the serving packages
(`serving_runners/`) emit — `contract_version`, `operation`, `status`, per-step results,
`metrics`, `error`, timing. That's deliberate: any tool that already understands a serving
package's verdict understands a DQ package's verdict for free.

### Where this lives in the code

- Skills: `workbench-skills/skills/data-quality-testing-gx` / `…-python` (generators +
  result loaders), `data-quality-failure-analysis`.
- Backend: `dq_test_generator.py`, `dq_test_executor.py`, `dq_package.py`,
  `stage_execution.py` (mode dispatch), `archetypes.py` (stages + workflow groups),
  `routers/summary.py` (`DQ_RULES_DETAIL_DPROD`).
- Canonical internals reference: the **DQ** sections of
  [`../workbench/backend/CLAUDE.md`](../workbench/backend/CLAUDE.md).
