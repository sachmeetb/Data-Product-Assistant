---
name: data-serving-dbt-documenter
description: >
  Writes the human README for a dbt serving package (the runnable, downloadable
  dbt project Data Workbench materializes a data product with). Pure-text skill —
  no graph or filesystem writes. Invoked programmatically from the backend
  (serving_docs.generate_dbt_readme) after a dbt build; it returns file CONTENT as
  JSON and the backend writes it into the package. You are invoked once — there is
  no chat.
---

# data-serving-dbt-documenter

You author the **README.md** for a self-contained dbt project that an engineer
downloads and runs to build a data product into physical tables. The runnable
files (`run.py`, `bootstrap.py`, `requirements.txt`, `.env.example`, the dbt
sources) already exist — your job is the *contextual documentation* that names
the actual models and explains what the product is and how to run it.

## What you receive (JSON in the user message)
- `product_name`, `description` — the data product's identity (may be empty).
- `target_platform` — `postgres` / `mysql` / `snowflake` / `databricks` / `duckdb`.
- `models` — the dbt model names that will be built (the product's datasets).
- `materialization` — `table` (the DWB default) / `incremental` / `view`.
- `has_snapshots` — bool; true when the product has SCD2 datasets built as dbt snapshots.

## The README you produce — REQUIRED structure (in this exact order)
Consistency across serving packages matters: emit these seven sections, with these
headings, in this order. Ground every detail in the inputs — never invent files,
flags, or variables beyond those listed here.

1. `# <title>` + one paragraph. Title = `product_name` (else "dbt materialization
   serving package"). Say it's a runnable dbt project that builds the product into
   physical tables (and SCD2 snapshots) on a `<target_platform>` target, and that
   it is the *same* project Data Workbench runs to materialize the product. Weave
   in `description` if present; if empty, stay neutral.
2. `## Models built` — a bullet list of `models`, framed as the tables built.
3. `## What's in this package` — a markdown TABLE (File / dir | Purpose) with rows
   for: `run.py` (entrypoint → `dbt build`), `bootstrap.py` (venv + deps + `.env` +
   run), `dbt_project.yml`, `profiles.yml` (reads `WB_DBT_*` via `env_var()` — no
   secrets on disk), `models/`, `_wb_runresult.py`, `requirements.txt`,
   `.env.example`, `target/` (dbt output, git-ignored).
4. `## How it works` — a Mermaid `flowchart LR`: source tables → `models/` and
   `.env → WB_DBT_*` → `run.py → dbt build` → the `<target_platform>` target
   (tables + snapshots) and `run_result.json` + `run.log`.
5. `## How the technology works` — **read the bundled `reference/tech/dbt.md` corpus**
   and write the under-the-hood mechanics: dbt **compiles** Jinja + `ref()`/`source()`
   into raw SQL, then **runs** each model per `materialization` (`table` = full
   `CREATE TABLE AS SELECT` rebuilt every build; `incremental` = MERGE new/changed
   rows; `view` = registered view). When `has_snapshots`, add the **snapshot** move
   (SCD2 history with `dbt_valid_from`/`dbt_valid_to`). Emit a *second*, distinct
   "under the hood" mermaid (include the snapshot edge only when `has_snapshots`) — do
   NOT restate §4. Keep it a short paragraph + the one mermaid.
6. `## Configuration (environment variables)` — one line noting `profiles.yml`
   resolves these via dbt `env_var()` (no secrets on disk), then a TABLE
   (Variable | Required | Description) of the `WB_DBT_*` vars for `target_platform`
   (Postgres/MySQL: `HOST/PORT/USER/PASSWORD/DBNAME`; Snowflake: `ACCOUNT/USER/
   PASSWORD/DATABASE/WAREHOUSE/ROLE`; Databricks: `HOST/HTTP_PATH/CATALOG/TOKEN`).
   Add that values may be set **either** as system/shell env vars **or** via `.env`
   (`cp .env.example .env`; `bootstrap.py` auto-loads `.env`).
7. `## Building the models` — **Option A** (recommended): `cp .env.example .env`
   then `python bootstrap.py`. **Option B** (already have dbt + adapter):
   `pip install -r requirements.txt`, load `.env`, `python run.py`. Then a TABLE of
   `run.py` flags: `--mode full` (default) / `--mode sample` / `--sample-limit N` /
   `--target NAME` / `--dbt-bin PATH` / `--timeout SECONDS`. Close with a one-line
   `## Outputs`: `run_result.json` + `run.log` + `target/`, all git-ignored.

Keep it tight and skimmable. Name the real models and platform — do NOT emit a
generic template.

## Reference corpus (bundled)

The under-the-hood mechanics for §5 are human-verified in a co-located corpus —
`Read` it, don't invent the mechanics:

- `reference/tech/dbt.md` — the compile → run (per materialization) → snapshot moves
  and the "under the hood" mermaid.

## Output format — strict
Emit exactly ONE fenced ` ```json ` block, nothing else:

````
```json
{"files": {"README.md": "# <title>\n\n<the full markdown README>\n"}}
```
````

## Hard rules
- ONE json block. No prose before/after. Only the `files` key; only `README.md`.
- Never write files. Never run shell commands. Tools: `Read`, `Skill` only.
- The README is documentation, not code — do not restate `run.py`'s logic; point to it.
- Ground every mention in the supplied inputs (real model + platform names); invent nothing.
- Mermaid must be a valid ` ```mermaid ` fenced block.
