"""Serving-package assembly + zip packaging.

A serving package is a self-contained, downloadable, runnable directory under
``<project>/serving/<mode>/``. It holds the core artifact (view DDL / dbt project
/ parquet) PLUS the deterministic runner (``run.py`` + helpers copied from
``serving_runners/``), ``requirements.txt``, ``.env.example``, ``.gitignore`` and
a README. Data Workbench executes serving by running the package's ``run.py``
(see ``serving_runtime.execute_package_runner``); the same directory is what an
engineer downloads and runs.

Deterministic files (runner, requirements, .env.example) are written here.
The README is enriched by ``serving_docs`` (skill, contextual) with a Python
fallback so a package is always complete even without an LLM.
"""
from __future__ import annotations

import io
import json
import shutil
import zipfile
from pathlib import Path
from typing import Callable, Optional

from fastapi import Response

from .config import BASE_PROJECT_DIR
from .serving_runtime import SERVING_RUNNERS_DIR


# Per-platform driver requirements for the runner (sqlparse is always needed for
# the DDL gate / statement split).
_DRIVER_REQUIREMENTS = {
    "postgres": ["psycopg2-binary>=2.9,<3.0"],
    "postgresql": ["psycopg2-binary>=2.9,<3.0"],
    "mysql": ["pymysql>=1.0,<2.0"],
    "snowflake": ["snowflake-connector-python>=3.0.0"],
    "databricks": ["databricks-sql-connector>=3.0.0"],
}

_GITIGNORE = "run_result.json\nrun.log\n.env\n__pycache__/\n*.pyc\n.dlt/\n"


def package_dir(project_code: str, mode: str) -> Path:
    """``<project>/serving/<mode>/`` — the on-disk package directory."""
    return BASE_PROJECT_DIR / project_code / "serving" / mode


# ── README building blocks (shared by the deterministic fallbacks) ───────────
# The documenter skills (serving_docs) mirror this same section order so a
# skill-authored and a fallback README read consistently per package type.

def _md_table(headers: list[str], rows: list[tuple]) -> str:
    """Render a GitHub-flavoured markdown table."""
    line = lambda cells: "| " + " | ".join(str(c) for c in cells) + " |"
    out = [line(headers), line(["---"] * len(headers))]
    out += [line(r) for r in rows]
    return "\n".join(out)


def _md_bullets(items: list[str]) -> str:
    return "\n".join(f"- {it}" for it in items) if items else "- _(none)_"


def _md_sql_details(blocks: list[tuple[str, str]]) -> str:
    """Render each ``(target_table, sql)`` as a GitHub/Gitea collapsible ``<details>``
    holding the full shaped SELECT in a fenced ``sql`` block. A blank line between
    ``<summary>`` and the fence is required for the fence to parse; the SQL is never
    truncated (holding the full query is the whole point of the collapsible)."""
    out = []
    for target_table, sql in blocks:
        out.append(
            f"<details><summary><code>{target_table}</code> — shaped SELECT</summary>\n\n"
            f"```sql\n{sql}\n```\n</details>"
        )
    return "\n\n".join(out)


def _artifact_changed(path: Path, new_content: str) -> bool:
    """True when ``path`` exists on disk with DIFFERENT content — i.e. the package's
    core artifact is being **regenerated**. A missing file is NOT "changed" (that's a
    first assemble, which ``_write_readme`` handles), and identical content (a plain
    re-assemble on download) is not a regeneration either."""
    try:
        return path.exists() and path.read_text(encoding="utf-8") != (new_content or "")
    except OSError:
        return False


def _invalidate_stale_readme(dest: Path, *, regenerated: bool) -> None:
    """Drop an existing README when the package is being **regenerated**, so the
    following ``_write_readme`` rewrites it from the CURRENT skill/fallback instead of
    preserving a now-stale one (the "clear README on (re)generate" contract — a
    documenter/fallback improvement reaches an already-authored package the next time
    its core artifact is regenerated). A first-time assemble or an unchanged
    re-assemble (a plain download) leaves any existing README intact."""
    if not regenerated:
        return
    readme = dest / "README.md"
    try:
        if readme.exists():
            readme.unlink()
    except OSError:
        pass


def _write_readme(dest: Path, provider: Optional[Callable[[], Optional[str]]],
                  fallback: Callable[[], str]) -> None:
    """Write ``README.md`` into ``dest``. If ``provider`` yields content (a
    skill-authored README), that wins. Otherwise an EXISTING README is preserved
    (so a skill README authored at deploy-time survives a later re-assemble on
    download), and only when none exists is the deterministic ``fallback`` written."""
    readme = None
    if provider is not None:
        try:
            readme = provider()
        except Exception:
            readme = None
    target = dest / "README.md"
    if readme and str(readme).strip():
        target.write_text(str(readme), encoding="utf-8")
    elif not target.exists():
        target.write_text(fallback(), encoding="utf-8")


def _how_to_run_section() -> str:
    """The env-loading conventions shared by every package (system vs .env)."""
    return (
        "## Setting configuration values\n\n"
        "Every value in the table above is read from a **process environment "
        "variable**. You can provide them two ways:\n\n"
        "- **System / shell level** — export them in your shell or CI before "
        "running (`export WB_...=...`), or set them in your container/orchestrator.\n"
        "- **`.env` file** — copy the bundled `.env.example` to `.env` and fill it "
        "in. Keep `.env` out of version control (it is already in `.gitignore`).\n\n"
        "Then load the `.env` before running:\n\n"
        "```bash\nset -a && . ./.env && set +a      # bash/zsh: export every line\n```\n"
    )


def _copy_runner(dest: Path, runner_src_name: str, *helpers: str) -> None:
    """Copy a mode runner (→ ``run.py``) + helper modules into ``dest``."""
    shutil.copyfile(SERVING_RUNNERS_DIR / runner_src_name, dest / "run.py")
    for h in helpers:
        shutil.copyfile(SERVING_RUNNERS_DIR / h, dest / h)


# ── "How the technology works" (the under-the-hood mechanics) ─────────────────
# A second, strategy-/platform-specific section that describes what the underlying
# engine ACTUALLY does on our behalf (the orchestration "How it works" diagram
# treats the engine as an opaque box). Mirrors the curated `reference/tech/` corpus
# bundled with each documenter skill, so the fallback README reads the same as the
# skill-authored one by construction. Concise: a short prose block + one labeled
# "under the hood" mermaid.

# Warehouse targets stage Parquet then COPY; relational targets load directly.
_WAREHOUSE_TARGETS = {"snowflake", "databricks", "redshift", "bigquery"}
_MIGRATION_STAGE_MECHANIC = {
    "snowflake": ("an internal Snowflake stage", "COPY INTO"),
    "databricks": ("a managed Unity Catalog volume", "COPY INTO"),
    "redshift": ("an S3 staging area", "COPY"),
    "bigquery": ("Google Cloud Storage", "a BigQuery load job"),
}
_WD_SEMANTICS = {
    "replace": "the target table is dropped and rebuilt on every load (full refresh)",
    "append": "each load appends new rows to the target table",
    "merge": "rows are upserted into the target on the primary key (`merge`)",
    "merge-on-pk": "rows are upserted into the target on the primary key (`merge`)",
}


def _tech_mechanics_section(
    kind: str, platform: Optional[str] = None, *,
    write_disposition: Optional[str] = None, materialization: Optional[str] = None,
    has_snapshots: bool = False, source_platform: Optional[str] = None,
) -> str:
    """Return the ``## How the technology works`` markdown (prose + an "under the
    hood" mermaid) for a package ``kind`` ∈ {view, dbt, lakehouse, migration},
    branched by platform / materialization / write disposition."""
    head = "## How the technology works\n\n"

    if kind == "view":
        p = (platform or "postgres").lower()
        quoting = "backtick-quoted" if p in ("mysql", "databricks") else "double-quoted"
        prose = (
            f"A virtual view is **zero-copy**: `run.py --apply` sends "
            f"`CREATE OR REPLACE VIEW` statements to the **{p}** target and **no data "
            "is moved or duplicated**. Each view is a stored query — every time it is "
            "read, the target engine re-executes the underlying `SELECT` against the "
            f"live source tables, so results are always current. Identifiers are "
            f"{quoting} and namespaced for {p}.\n\n"
        )
        diagram = (
            "```mermaid\nflowchart LR\n"
            '  DDL["view.sql<br/>CREATE OR REPLACE VIEW"] -->|"run.py --apply"| ENG["'
            f'{p} engine<br/>(stores the query, no data)"]\n'
            '  Q["reader query"] --> ENG\n'
            '  ENG -->|"executes SELECT at read time"| BASE[("live source tables")]\n'
            "```\n"
        )
        return head + prose + diagram

    if kind == "dbt":
        p = (platform or "postgres").lower()
        mat = (materialization or "table").lower()
        if mat == "incremental":
            run_line = ("**run** — each model is `incremental`: new/changed rows are "
                        "`MERGE`d into the existing table instead of a full rebuild")
        elif mat == "view":
            run_line = ("**run** — each model is registered as a database `view` "
                        "(recomputed on read)")
        else:
            run_line = ("**run** — each model is materialized as a `table` (a full "
                        "`CREATE TABLE AS SELECT`, rebuilt on every build)")
        snap = (" For any SCD2 dataset, dbt **snapshots** track row changes over time "
                "into a snapshot table (`dbt_valid_from` / `dbt_valid_to`)."
                if has_snapshots else "")
        prose = (
            f"dbt turns the SQL in `models/` into physical relations on the **{p}** "
            "target in three moves: **compile** — render the Jinja + `ref()`/`source()` "
            f"into raw {p} SQL; {run_line}.{snap} Per-model status is written to "
            "`target/run_results.json`.\n\n"
        )
        lines = [
            "```mermaid\nflowchart LR",
            '  M["models/*.sql"] -->|compile| C["compiled SQL"]',
            f'  C -->|"run"| T[("{p} tables")]',
        ]
        if has_snapshots:
            lines.append('  S["snapshots/"] -->|"snapshot (SCD2 history)"| H[("snapshot tables")]')
        lines.append("```\n")
        return head + prose + "\n".join(lines)

    if kind == "lakehouse":
        src = (source_platform or "").lower()
        src_phrase = f"the live **{src}** source" if src else "the live source"
        prose = (
            f"The export runs entirely inside **DuckDB**. `run.py --mode export` "
            f"ATTACHes {src_phrase}, then for each dataset runs "
            "`COPY (SELECT …) TO 'data/<name>.parquet' (FORMAT PARQUET)` — streaming "
            "the result straight to columnar **Parquet**. It registers a DuckDB view "
            "over each Parquet file in `catalog.duckdb`, and verifies the row count "
            "with **two independent engines** (DuckDB + pyarrow) before writing a "
            "TransferBatch v1 manifest.\n\n"
        )
        diagram = (
            "```mermaid\nflowchart LR\n"
            '  SRC[("source")] -->|ATTACH| DK["DuckDB"]\n'
            '  DK -->|"COPY (SELECT …) TO … (FORMAT PARQUET)"| PQ["data/*.parquet"]\n'
            '  PQ -->|"register view"| CAT["catalog.duckdb"]\n'
            '  PQ -->|"DuckDB + pyarrow row-count verify"| MAN["*__manifest.json"]\n'
            "```\n"
        )
        return head + prose + diagram

    if kind == "migration":
        tp = (platform or "postgres").lower()
        sp = (source_platform or "").lower()
        src_phrase = f"the **{sp}** source" if sp else "the source"
        wd = (write_disposition or "replace").lower()
        wd_text = _WD_SEMANTICS.get(wd, _WD_SEMANTICS["replace"])
        if tp in _WAREHOUSE_TARGETS:
            stage, copy_verb = _MIGRATION_STAGE_MECHANIC.get(
                tp, ("an internal stage", "COPY INTO"))
            load_line = (
                f"**load** — the Parquet is staged into {stage} and pulled into the "
                f"target in a single bulk `{copy_verb}` (not row-by-row `INSERT`s)")
            diagram = (
                "```mermaid\nflowchart LR\n"
                f'  SRC[("{sp or "source"}")] -->|"extract · sql_database"| '
                'NORM["normalize<br/>rows → Parquet"]\n'
                f'  NORM -->|stage| STG[["{stage}"]]\n'
                f'  STG -->|"{copy_verb}"| TGT[("{tp} target")]\n'
                "```\n"
            )
        else:
            load_line = (
                "**load** — the Parquet load files are bulk-loaded directly into the "
                "target; no external stage is involved")
            diagram = (
                "```mermaid\nflowchart LR\n"
                f'  SRC[("{sp or "source"}")] -->|"extract · sql_database"| '
                'NORM["normalize<br/>rows → Parquet load files"]\n'
                f'  NORM -->|"bulk direct load"| TGT[("{tp} target")]\n'
                "```\n"
            )
        prose = (
            "`dlt` does not issue row-by-row `INSERT`s. It runs a three-stage "
            f"pipeline: **extract** — the `sql_database` source reads {src_phrase} "
            "over SQLAlchemy; **normalize** — rows are written to local **Parquet** "
            f"load files; {load_line}. The write disposition is **{wd}** — {wd_text}."
            "\n\n"
        )
        return head + prose + diagram

    return ""


# ── view package ────────────────────────────────────────────────────────────


def _view_env_example(platform: str) -> str:
    p = (platform or "postgres").lower()
    if p in ("postgres", "postgresql"):
        return (
            "# Target database for the view deploy.\n"
            "# Either a full DSN…\n"
            "WB_TARGET_DSN=postgresql://user:password@host:5432/dbname\n"
            "# …or discrete parts (DSN wins if both set):\n"
            "# WB_TARGET_HOST=localhost\n# WB_TARGET_PORT=5432\n"
            "# WB_TARGET_USER=postgres\n# WB_TARGET_PASSWORD=\n# WB_TARGET_DBNAME=postgres\n"
        )
    if p == "mysql":
        return (
            "# Target MySQL for the view deploy.\n"
            "WB_TARGET_HOST=localhost\nWB_TARGET_PORT=3306\n"
            "WB_TARGET_USER=root\nWB_TARGET_PASSWORD=\nWB_TARGET_DBNAME=\n"
        )
    if p == "snowflake":
        return (
            "WB_TARGET_HOST=<account>\nWB_TARGET_USER=\nWB_TARGET_PASSWORD=\n"
            "WB_TARGET_DBNAME=\nWB_TARGET_WAREHOUSE=\nWB_TARGET_ROLE=\n"
        )
    if p == "databricks":
        return "WB_TARGET_HOST=\nWB_TARGET_HTTP_PATH=\nWB_TARGET_TOKEN=\n"
    return "WB_TARGET_PLATFORM={0}\n".format(p)


def _view_env_rows(platform: str) -> list[tuple]:
    """Env-var documentation rows (name, required, description) for the view runner."""
    p = (platform or "postgres").lower()
    rows = [("`WB_TARGET_PLATFORM`", "no",
             f"Target engine. Defaults to `{p}`.")]
    if p in ("postgres", "postgresql"):
        rows += [
            ("`WB_TARGET_DSN`", "one of", "Full DSN, e.g. `postgresql://user:pw@host:5432/db`. Wins if set."),
            ("`WB_TARGET_HOST`", "or", "Host (when not using a DSN)."),
            ("`WB_TARGET_PORT`", "no", "Port (default `5432`)."),
            ("`WB_TARGET_USER`", "yes*", "Username."),
            ("`WB_TARGET_PASSWORD`", "yes*", "Password."),
            ("`WB_TARGET_DBNAME`", "yes*", "Database name."),
        ]
    elif p == "mysql":
        rows += [
            ("`WB_TARGET_HOST`", "yes", "Host."), ("`WB_TARGET_PORT`", "no", "Port (default `3306`)."),
            ("`WB_TARGET_USER`", "yes", "Username."), ("`WB_TARGET_PASSWORD`", "yes", "Password."),
            ("`WB_TARGET_DBNAME`", "yes", "Database name."),
        ]
    elif p == "snowflake":
        rows += [
            ("`WB_TARGET_HOST`", "yes", "Account identifier."), ("`WB_TARGET_USER`", "yes", "Username."),
            ("`WB_TARGET_PASSWORD`", "yes", "Password."), ("`WB_TARGET_DBNAME`", "yes", "Database."),
            ("`WB_TARGET_WAREHOUSE`", "yes", "Warehouse."), ("`WB_TARGET_ROLE`", "no", "Role."),
        ]
    elif p == "databricks":
        rows += [
            ("`WB_TARGET_HOST`", "yes", "Workspace host."),
            ("`WB_TARGET_HTTP_PATH`", "yes", "SQL warehouse HTTP path."),
            ("`WB_TARGET_TOKEN`", "yes", "Personal access token."),
        ]
    return rows


def _fallback_view_readme(project_code, platform, view_schema, view_names,
                          product_name=None) -> str:
    title = product_name or project_code
    view_list = _md_bullets([f"`{view_schema}.{n.rsplit('.', 1)[-1]}`" for n in view_names])
    inventory = _md_table(["File", "Purpose"], [
        ("`view.sql`", "The `CREATE OR REPLACE VIEW` DDL this package deploys."),
        ("`package.json`", "Manifest: mode, platform, view schema/names, namespace descriptor."),
        ("`run.py`", "Entrypoint. Applies the DDL to the target. **This is what you run.**"),
        ("`_deploy_core.py`", "Deploy engine (statement split + apply + view-recreate recovery)."),
        ("`_wb_runresult.py`", "Writes the structured `run_result.json` + `run.log`."),
        ("`requirements.txt`", "Python deps (`sqlparse` + the target driver)."),
        ("`.env.example`", "Template for the target connection env vars — copy to `.env`."),
        ("`.gitignore`", "Excludes `.env` + per-run artifacts."),
    ])
    env_table = _md_table(["Variable", "Required", "Description"], _view_env_rows(platform))
    # The DSN-vs-discrete footnote only applies to the Postgres env rows (the sole
    # rows that carry the `*` marker). Suppress it for every other target so a
    # Databricks/Snowflake/MySQL package doesn't carry a stray Postgres reference.
    pg_footnote = (
        "`*` For Postgres, supply **either** `WB_TARGET_DSN` **or** the discrete "
        "`WB_TARGET_HOST/PORT/USER/PASSWORD/DBNAME` parts (the DSN wins if both are set).\n\n"
        if (platform or "postgres").lower() in ("postgres", "postgresql")
        else ""
    )
    return (
        f"# {title} — virtual view serving package\n\n"
        f"Deploys **{len(view_names)} view(s)** to a **{platform}** database as "
        "`CREATE OR REPLACE VIEW` statements. This is the *same* package Data "
        "Workbench runs to deploy the product, so what you run here is byte-identical "
        "to production.\n\n"
        "## Views deployed\n" + view_list + f"\n\nAll views are created in schema `{view_schema}`.\n\n"
        "## What's in this package\n" + inventory + "\n\n"
        "## How it works\n\n"
        "```mermaid\nflowchart LR\n"
        '  ENV[".env → WB_TARGET_*"] --> R["run.py --apply"]\n'
        '  SQL["view.sql"] --> R\n'
        f'  R --> DB[("{platform} target<br/>{view_schema}.* views")]\n'
        '  R --> OUT["run_result.json + run.log"]\n```\n\n'
        + _tech_mechanics_section("view", platform) + "\n"
        "## Configuration (environment variables)\n\n" + env_table + "\n\n"
        + pg_footnote
        + _how_to_run_section() + "\n"
        "## Running the deploy\n\n"
        "```bash\npython -m venv .venv && . .venv/bin/activate   # Windows: .venv\\Scripts\\activate\n"
        "pip install -r requirements.txt\n"
        "cp .env.example .env            # then edit .env with your target credentials\n"
        "set -a && . ./.env && set +a    # load the connection into the environment\n"
        "python run.py --apply\n```\n\n"
        "**`run.py` flags**\n\n"
        + _md_table(["Flag", "Effect"], [
            ("`--apply`", "Execute the DDL against the target. Without it, `run.py` validates only (dry run)."),
            ("`--ddl-file PATH`", "Deploy a DDL file other than the bundled `view.sql`."),
        ]) + "\n\n"
        "## Outputs\n\n"
        "- `run_result.json` — machine-readable status, statements executed, timings, any error class.\n"
        "- `run.log` — line-by-line JSONL log of the run.\n\n"
        "Both are regenerated every run and are git-ignored.\n"
    )


def assemble_view_package(
    project_code: str, *, ddl: str, view_schema: str, view_names: list[str],
    platform: str, product_name: Optional[str] = None,
    readme_provider: Optional[Callable[[], Optional[str]]] = None,
) -> Path:
    """Assemble ``<project>/serving/view/`` for a virtual-view product."""
    dest = package_dir(project_code, "view")
    dest.mkdir(parents=True, exist_ok=True)
    # Clear a stale README when the DDL is being regenerated (e.g. the Data Serving
    # stage re-ran and the view SELECT changed) so it's rewritten, not preserved.
    _invalidate_stale_readme(dest, regenerated=_artifact_changed(dest / "view.sql", ddl or ""))
    (dest / "view.sql").write_text(ddl or "", encoding="utf-8")
    # Serialise the platform NamespaceModel into the manifest so the stdlib-only
    # runner reproduces quoting / session-setup / create-namespace without
    # importing the backend. Best-effort: an unknown platform (fail-closed model)
    # just omits it and the runner falls back to per-platform defaults.
    try:
        from .platform.namespace import get_namespace_model
        namespace_descriptor = get_namespace_model(platform).to_descriptor()
    except Exception:
        namespace_descriptor = None
    (dest / "package.json").write_text(json.dumps({
        "mode": "virtual_view", "platform": platform, "view_schema": view_schema,
        "view_names": list(view_names or []), "ddl_file": "view.sql",
        "namespace": namespace_descriptor,
    }, indent=2), encoding="utf-8")
    _copy_runner(dest, "run_deploy.py", "_wb_runresult.py", "_deploy_core.py")
    reqs = ["sqlparse>=0.4"] + _DRIVER_REQUIREMENTS.get((platform or "postgres").lower(), [])
    (dest / "requirements.txt").write_text("\n".join(reqs) + "\n", encoding="utf-8")
    (dest / ".env.example").write_text(_view_env_example(platform), encoding="utf-8")
    (dest / ".gitignore").write_text(_GITIGNORE, encoding="utf-8")
    _write_readme(dest, readme_provider, lambda: _fallback_view_readme(
        project_code, platform, view_schema, view_names, product_name))
    return dest


# ── lakehouse package ─────────────────────────────────────────────────────────


_LAKEHOUSE_ENV_EXAMPLE = (
    "# Live source the export reads from (only needed for `python run.py --mode export`).\n"
    "WB_SOURCE_PLATFORM=postgres\n"
    "WB_SOURCE_DSN=postgresql://user:password@host:5432/dbname\n"
    "# MySQL example: WB_SOURCE_PLATFORM=mysql / WB_SOURCE_DSN=mysql://user:pw@host:3306/db\n"
    "# `python run.py --mode query` needs no source — it reads catalog.duckdb.\n"
)

_LAKEHOUSE_QUERY_PY = '''\
#!/usr/bin/env python3
"""Read the exported lakehouse: list views in catalog.duckdb and sample rows.
Needs no source connection. Usage: python query.py [--limit N]"""
import argparse
from pathlib import Path
import duckdb

CATALOG = Path(__file__).resolve().parent / "catalog.duckdb"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=5)
    args = ap.parse_args()
    if not CATALOG.exists():
        raise SystemExit("catalog.duckdb not found — run `python run.py --mode export` first.")
    con = duckdb.connect(str(CATALOG), read_only=True)
    views = [r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_type='VIEW' "
        "ORDER BY table_name").fetchall()]
    print(f"{len(views)} view(s): {', '.join(views)}")
    for v in views:
        n = con.execute(f'SELECT count(*) FROM "{v}"').fetchone()[0]
        print(f"\\n== {v} ({n} rows) ==")
        cols = [d[0] for d in con.execute(f'SELECT * FROM "{v}" LIMIT 0').description]
        print(" | ".join(cols))
        for row in con.execute(f'SELECT * FROM "{v}" LIMIT {args.limit}').fetchall():
            print(" | ".join(str(c) for c in row))
    con.close()


if __name__ == "__main__":
    main()
'''


def _lakehouse_explore_sql(models: list[dict]) -> str:
    lines = ["-- Explore the exported product with DuckDB:",
             "--   duckdb catalog.duckdb   (or: SELECT * FROM read_parquet('data/*.parquet'))", ""]
    for m in models:
        safe = m.get("model_name") or m.get("physical_name")
        lines.append(f'SELECT * FROM "{safe}" LIMIT 20;')
    return "\n".join(lines) + "\n"


def _fallback_lakehouse_readme(project_code, models, product_name=None,
                               source_platform=None) -> str:
    title = product_name or project_code
    names = [m.get("model_name") or m.get("physical_name") for m in models]
    first = names[0] if names else "<dataset>"
    dataset_list = _md_bullets([f"`{n}`" for n in names])
    inventory = _md_table(["File", "Purpose"], [
        ("`run.py`", "Entrypoint. `--mode export` snapshots the source → Parquet; `--mode query` reads it back."),
        ("`query.py`", "Offline reader — lists DuckDB views and samples rows. No source needed."),
        ("`explore.sql`", "Ready-to-run `SELECT`s for `duckdb catalog.duckdb`."),
        ("`models.json`", "The compiled DuckDB SELECT body per dataset (drives the export)."),
        ("`_wb_runresult.py`", "Writes the structured `run_result.json` + `run.log`."),
        ("`requirements.txt`", "Python deps (`duckdb`, `pyarrow`)."),
        ("`.env.example`", "Template for the source connection (export mode only) — copy to `.env`."),
        ("`data/*.parquet`", "The exported data (present after `--mode export`)."),
        ("`catalog.duckdb`", "DuckDB catalog with a view over each Parquet file (present after export)."),
        ("`data/<name>__manifest.json`", "TransferBatch v1 delivery manifest per dataset (row counts, checksums, schema)."),
    ])
    env_table = _md_table(["Variable", "Required", "Description"], [
        ("`WB_SOURCE_PLATFORM`", "export only", "Source engine, e.g. `postgres` / `mysql`. Default `postgres`."),
        ("`WB_SOURCE_DSN`", "export only", "Source DSN, e.g. `postgresql://user:pw@host:5432/db`."),
        ("`WB_SOURCE_VIEW_SCHEMA`", "no", "Schema the served views live in (default `public`)."),
    ])
    return (
        f"# {title} — lakehouse (Parquet + DuckDB) serving package\n\n"
        "A self-contained **Parquet** snapshot of the product plus a **DuckDB** "
        "catalog. Two independent uses: query the exported data with zero source "
        "access, or reproduce the export against a live source. This is the same "
        "package Data Workbench runs to serve the product.\n\n"
        "## Datasets\n" + dataset_list + "\n\n"
        "## What's in this package\n" + inventory + "\n\n"
        "## How it works\n\n"
        "```mermaid\nflowchart LR\n"
        '  SRC[("live source")] -->|"run.py --mode export"| P["data/*.parquet<br/>catalog.duckdb"]\n'
        '  ENV[".env → WB_SOURCE_*"] -.-> SRC\n'
        '  P -->|"query.py / explore.sql"| Q["analysis (offline)"]\n'
        '  P --> M["*__manifest.json<br/>(TransferBatch v1)"]\n```\n\n'
        + _tech_mechanics_section("lakehouse", source_platform=source_platform) + "\n"
        "## Use 1 — query the data (no source needed)\n\n"
        "```bash\npython -m venv .venv && . .venv/bin/activate\n"
        "pip install -r requirements.txt\n"
        "python query.py --limit 10          # lists DuckDB views + samples rows\n"
        "# or explore interactively:\nduckdb catalog.duckdb\n```\n\n"
        f"Example query:\n\n```sql\nSELECT * FROM \"{first}\" LIMIT 20;\n```\n\n"
        "**`query.py` flags:** `--limit N` (rows sampled per view, default 5).\n\n"
        "## Use 2 — reproduce the export (needs source access)\n\n"
        "This reads a **live source**, so it needs connection env vars:\n\n"
        + env_table + "\n\n" + _how_to_run_section() + "\n"
        "```bash\ncp .env.example .env            # then edit with your source connection\n"
        "set -a && . ./.env && set +a\npython run.py --mode export\n```\n\n"
        "**`run.py` flags**\n\n"
        + _md_table(["Flag", "Effect"], [
            ("`--mode export`", "Snapshot the source into `data/*.parquet` + `catalog.duckdb` (default)."),
            ("`--mode query`", "Read the local catalog back (same as `query.py`)."),
            ("`--compression NAME`", "Parquet codec (default `snappy`)."),
            ("`--sample`", "Cap rows per dataset (a sample export)."),
            ("`--sample-limit N`", "Row cap when `--sample` is set (default 100)."),
        ]) + "\n\n"
        "## Outputs & verification\n\n"
        "- `data/*.parquet` + `catalog.duckdb` — the exported product.\n"
        "- `data/<name>__manifest.json` — a **TransferBatch v1** manifest per dataset "
        "(row counts, checksums, schema fingerprint) for delivery verification.\n"
        "- `run_result.json` + `run.log` — per-run status (git-ignored).\n"
    )


def assemble_lakehouse_package(
    export_dir: Path, *, project_code: str, models: list[dict],
    product_name: Optional[str] = None, source_platform: Optional[str] = None,
    readme_provider: Optional[Callable[[], Optional[str]]] = None,
) -> Path:
    """Assemble the runnable lakehouse package IN the export dir (parquet lands
    in ``data/`` when the runner runs). ``models`` = the compiled DuckDB bodies
    ``[{physical_name, model_name, select_body}]``."""
    export_dir.mkdir(parents=True, exist_ok=True)
    # Clear a stale README when the exported model set is being regenerated so it's
    # rewritten from the current skill/fallback, not preserved.
    models_json = json.dumps(models, indent=2)
    _invalidate_stale_readme(export_dir, regenerated=_artifact_changed(export_dir / "models.json", models_json))
    (export_dir / "models.json").write_text(models_json, encoding="utf-8")
    _copy_runner(export_dir, "run_lakehouse.py", "_wb_runresult.py")
    (export_dir / "query.py").write_text(_LAKEHOUSE_QUERY_PY, encoding="utf-8")
    (export_dir / "explore.sql").write_text(_lakehouse_explore_sql(models), encoding="utf-8")
    (export_dir / "requirements.txt").write_text(
        "duckdb>=1.0,<2.0\npyarrow>=14.0\n", encoding="utf-8")
    (export_dir / ".env.example").write_text(_LAKEHOUSE_ENV_EXAMPLE, encoding="utf-8")
    (export_dir / ".gitignore").write_text(_GITIGNORE, encoding="utf-8")
    _write_readme(export_dir, readme_provider,
                  lambda: _fallback_lakehouse_readme(project_code, models, product_name,
                                                     source_platform))
    return export_dir


# ── migration package (dmig — DLT source→target) ─────────────────────────────

# dlt destination "extras" per target platform (installed as dlt[<extra>]).
_DLT_EXTRAS = {
    "postgres": "dlt[postgres]>=1.0", "postgresql": "dlt[postgres]>=1.0",
    "mysql": "dlt[sqlalchemy]>=1.0", "snowflake": "dlt[snowflake]>=1.0",
    "databricks": "dlt[databricks]>=1.0",
    "sqlserver": "dlt[mssql]>=1.0", "mssql": "dlt[mssql]>=1.0", "azure_sql": "dlt[mssql]>=1.0",
    "oracle": "dlt[sqlalchemy]>=1.0",
}
# SQLAlchemy driver the sql_database source needs to READ each source platform.
_SOURCE_DRIVER_REQUIREMENTS = {
    "postgres": ["psycopg2-binary>=2.9,<3.0"], "postgresql": ["psycopg2-binary>=2.9,<3.0"],
    "mysql": ["pymysql>=1.0,<2.0"],
    # snowflake-sqlalchemy provides the `snowflake://` SQLAlchemy dialect the
    # source engine reads through (a PAT authenticates as a plain password).
    "snowflake": ["snowflake-sqlalchemy>=1.5"],
    "sqlserver": ["pyodbc>=5.0"], "mssql": ["pyodbc>=5.0"], "azure_sql": ["pyodbc>=5.0"],
    "oracle": ["oracledb>=2.0"],
}


def _migration_requirements(source_platform: str, target_platform: str) -> str:
    reqs = ["sqlalchemy>=2.0,<3.0",
            _DLT_EXTRAS.get((target_platform or "postgres").lower(), "dlt>=1.0")]
    reqs += _SOURCE_DRIVER_REQUIREMENTS.get((source_platform or "postgres").lower(), [])
    # de-dup while preserving order
    seen, out = set(), []
    for r in reqs:
        if r not in seen:
            seen.add(r); out.append(r)
    return "\n".join(out) + "\n"


def _migration_target_env(platform: str) -> str:
    p = (platform or "postgres").lower()
    if p in ("postgres", "postgresql"):
        return ("WB_TARGET_PLATFORM=postgres\n"
                "WB_TARGET_DSN=postgresql://user:password@host:5432/dbname\n"
                "# …or discrete parts (DSN wins):\n"
                "# WB_TARGET_HOST=  WB_TARGET_PORT=5432  WB_TARGET_USER=  WB_TARGET_PASSWORD=  WB_TARGET_DBNAME=\n")
    if p == "mysql":
        return ("WB_TARGET_PLATFORM=mysql\nWB_TARGET_HOST=\nWB_TARGET_PORT=3306\n"
                "WB_TARGET_USER=\nWB_TARGET_PASSWORD=\nWB_TARGET_DBNAME=\n")
    if p == "snowflake":
        return ("WB_TARGET_PLATFORM=snowflake\nWB_TARGET_ACCOUNT=\nWB_TARGET_USER=\n"
                "WB_TARGET_PASSWORD=\nWB_TARGET_DBNAME=\nWB_TARGET_WAREHOUSE=\nWB_TARGET_ROLE=\n")
    if p == "databricks":
        return ("WB_TARGET_PLATFORM=databricks\nWB_TARGET_HOST=\nWB_TARGET_HTTP_PATH=\n"
                "WB_TARGET_CATALOG=\nWB_TARGET_TOKEN=\n")
    if p in ("sqlserver", "mssql", "azure_sql"):
        return ("WB_TARGET_PLATFORM=sqlserver\nWB_TARGET_HOST=\nWB_TARGET_PORT=1433\n"
                "WB_TARGET_USER=\nWB_TARGET_PASSWORD=\nWB_TARGET_DBNAME=\n"
                "# WB_TARGET_ODBC_DRIVER=ODBC Driver 18 for SQL Server\n")
    if p == "oracle":
        return ("WB_TARGET_PLATFORM=oracle\nWB_TARGET_HOST=\nWB_TARGET_PORT=1521\n"
                "WB_TARGET_USER=\nWB_TARGET_PASSWORD=\nWB_TARGET_DBNAME=  # service name\n")
    return "WB_TARGET_PLATFORM=postgres\nWB_TARGET_DSN=\n"


def _migration_env_example(source_platform: str, target_platform: str) -> str:
    sp = (source_platform or "postgres").lower()
    if sp == "snowflake":
        src = ("# ── Source (read) ──\n"
               "WB_SOURCE_PLATFORM=snowflake\n"
               "WB_SOURCE_ACCOUNT=   # account identifier (falls back to WB_SOURCE_HOST)\n"
               "WB_SOURCE_USER=\n"
               "WB_SOURCE_PASSWORD=   # a Snowflake PAT authenticates as a plain password\n"
               "WB_SOURCE_DBNAME=\nWB_SOURCE_SCHEMA=\nWB_SOURCE_WAREHOUSE=\nWB_SOURCE_ROLE=\n")
    else:
        src = ("# ── Source (read) ──\n"
               f"WB_SOURCE_PLATFORM={sp}\n"
               "WB_SOURCE_DSN=" + ("postgresql://user:password@host:5432/dbname\n"
                                   if sp in ("postgres", "postgresql")
                                   else "mysql://user:password@host:3306/dbname\n"))
    return src + "\n# ── Target (write) ──\n" + _migration_target_env(target_platform)


def _migration_target_env_rows(platform: str) -> list[tuple]:
    p = (platform or "postgres").lower()
    if p == "snowflake":
        return [("`WB_TARGET_ACCOUNT`", "yes", "Snowflake account identifier."),
                ("`WB_TARGET_USER` / `WB_TARGET_PASSWORD`", "yes", "Credentials."),
                ("`WB_TARGET_DBNAME`", "yes", "Target database."),
                ("`WB_TARGET_WAREHOUSE`", "yes", "Warehouse."), ("`WB_TARGET_ROLE`", "no", "Role.")]
    if p == "databricks":
        return [("`WB_TARGET_HOST`", "yes", "Workspace host."),
                ("`WB_TARGET_HTTP_PATH`", "yes", "SQL warehouse HTTP path."),
                ("`WB_TARGET_CATALOG`", "no", "Unity catalog."),
                ("`WB_TARGET_TOKEN`", "yes", "Personal access token.")]
    if p in ("sqlserver", "mssql", "azure_sql"):
        return [("`WB_TARGET_HOST` / `WB_TARGET_PORT`", "yes", "Server host + port (default 1433)."),
                ("`WB_TARGET_USER` / `WB_TARGET_PASSWORD`", "yes", "SQL login."),
                ("`WB_TARGET_DBNAME`", "yes", "Target database."),
                ("`WB_TARGET_ODBC_DRIVER`", "no", "ODBC driver name (default 'ODBC Driver 18 for SQL Server').")]
    if p == "oracle":
        return [("`WB_TARGET_HOST` / `WB_TARGET_PORT`", "yes", "Listener host + port (default 1521)."),
                ("`WB_TARGET_USER` / `WB_TARGET_PASSWORD`", "yes", "Schema credentials."),
                ("`WB_TARGET_DBNAME`", "yes", "Service name.")]
    return [("`WB_TARGET_DSN`", "yes*", "Full target DSN (or the discrete parts below)."),
            ("`WB_TARGET_HOST/PORT/USER/PASSWORD/DBNAME`", "yes*", "Discrete parts (DSN wins).")]


def _fallback_migration_readme(project_code, spec, product_name=None) -> str:
    title = product_name or project_code
    sp = spec.get("source_platform", "postgres")
    tp = spec.get("target_platform", "postgres")
    datasets = spec.get("datasets", [])
    names = [d.get("source_table", "?") for d in datasets]
    ds_list = _md_bullets([f"`{n}`" for n in names]) if names else "- _(generated at build time)_"
    inventory = _md_table(["File", "Purpose"], [
        ("`run.py`", "Entrypoint. `--mode load` migrates source→target; `--mode verify` reconciles row counts; `--mode plan` dry-runs."),
        ("`migration.json`", "The framework-neutral migration contract (per-dataset source→target + write disposition)."),
        ("`_wb_runresult.py`", "Writes the structured `run_result.json` + `run.log`."),
        ("`requirements.txt`", "`dlt` (+ target extra) + SQLAlchemy source driver."),
        ("`.env.example`", "Template for the source + target connections — copy to `.env`."),
        ("`manifests/*.json`", "TransferBatch v1 manifest per dataset (row counts, schema fingerprint) — written on load."),
    ])
    src_row = ("`WB_SOURCE_PLATFORM`", "yes", f"Source engine (`{sp}`).")
    src_dsn = ("`WB_SOURCE_DSN`", "yes", "Source DSN (or discrete `WB_SOURCE_*` parts).")
    env_table = _md_table(["Variable", "Required", "Description"],
                          [src_row, src_dsn] + _migration_target_env_rows(tp))
    return (
        f"# {title} — data-migration package ({sp} → {tp})\n\n"
        "A self-contained, runnable **DLT** pipeline that lift-and-shifts the source "
        "tables into the target platform. This is the same package Data Workbench runs "
        "to execute the migration.\n\n"
        "## Datasets\n" + ds_list + "\n\n"
        "## What's in this package\n" + inventory + "\n\n"
        "## How it works\n\n"
        "```mermaid\nflowchart LR\n"
        f'  SRC[("{sp} source")] -->|"run.py --mode load"| DLT["dlt pipeline"]\n'
        f'  DLT --> TGT[("{tp} target")]\n'
        '  DLT --> M["manifests/*.json<br/>(TransferBatch v1)"]\n'
        '  ENV[".env → WB_SOURCE_* / WB_TARGET_*"] -.-> DLT\n```\n\n'
        + _tech_mechanics_section(
            "migration", tp, write_disposition=spec.get("write_disposition"),
            source_platform=sp) + "\n"
        "## Configuration\n\n" + env_table + "\n\n" + _how_to_run_section() + "\n"
        "## Usage\n\n"
        "```bash\npython -m venv .venv && . .venv/bin/activate\n"
        "pip install -r requirements.txt\n"
        "cp .env.example .env            # then edit with your source + target connections\n"
        "set -a && . ./.env && set +a\n"
        "python run.py --mode load       # migrate\n"
        "python run.py --mode verify     # reconcile source vs target row counts\n```\n\n"
        "**`run.py` flags**\n\n"
        + _md_table(["Flag", "Effect"], [
            ("`--mode load`", "Extract from the source and load into the target (default)."),
            ("`--mode verify`", "Count source vs target rows and report per-table pass/fail."),
            ("`--mode plan`", "Print the planned source→target moves without touching anything."),
        ]) + "\n\n"
        "## Outputs\n\n"
        "- Target tables in the configured target schema.\n"
        "- `manifests/<name>__manifest.json` — a **TransferBatch v1** manifest per dataset.\n"
        "- `run_result.json` + `run.log` — per-run status (git-ignored).\n"
    )


def assemble_migration_package(
    *, project_code: str, spec: dict,
    product_name: Optional[str] = None,
    readme_provider: Optional[Callable[[], Optional[str]]] = None,
) -> Path:
    """Assemble the runnable migration package under ``<project>/serving/migration/``.

    ``spec`` is the framework-neutral migration contract
    (``{source_platform, target_platform, target_schema, write_disposition, datasets:[...]}``)
    the generator skill produced. Mirrors ``assemble_lakehouse_package``: writes
    ``migration.json``, copies the ``run_migration.py`` runner (→ ``run.py``) +
    ``_wb_runresult.py``, and writes requirements / env-example / gitignore / README.
    """
    dest = package_dir(project_code, "migration")
    dest.mkdir(parents=True, exist_ok=True)
    # Clear a stale README when the migration contract is being regenerated (e.g. the
    # Generate Migration Pipeline stage re-ran with a new spec) so it's rewritten.
    spec_json = json.dumps(spec, indent=2)
    _invalidate_stale_readme(dest, regenerated=_artifact_changed(dest / "migration.json", spec_json))
    (dest / "migration.json").write_text(spec_json, encoding="utf-8")
    _copy_runner(dest, "run_migration.py", "_wb_runresult.py")
    (dest / "requirements.txt").write_text(
        _migration_requirements(spec.get("source_platform", "postgres"),
                                spec.get("target_platform", "postgres")), encoding="utf-8")
    (dest / ".env.example").write_text(
        _migration_env_example(spec.get("source_platform", "postgres"),
                               spec.get("target_platform", "postgres")), encoding="utf-8")
    (dest / ".gitignore").write_text(_GITIGNORE, encoding="utf-8")
    _write_readme(dest, readme_provider,
                  lambda: _fallback_migration_readme(project_code, spec, product_name))
    return dest


# ── code-migration package ────────────────────────────────────────────────────

def _fallback_code_migration_readme(project_code, meta: dict, product_name=None) -> str:
    """Deterministic README for the code-migration package: what was converted,
    the original→spec→design→new-code lineage, and how to inspect it."""
    title = product_name or project_code
    src_p = meta.get("source_platform", "the legacy platform")
    tgt_p = meta.get("target_platform", "the target platform")
    kind = meta.get("artifact_kind", "sql_script")
    buckets = meta.get("buckets", {})
    old_files = meta.get("old_files", [])
    new_files = meta.get("new_files", [])
    inv = _md_table(
        ["Path", "Purpose"],
        [
            ("old/", "the original legacy code, exactly as imported (immutable)"),
            ("new/", "the converted target-platform code"),
            ("codespec.json", "the reviewed, use-case-focused specification"),
            ("design.md", "the forward-engineering design notes (if produced)"),
            ("conversion.json", "per-construct conversion report (converted / manual_action / unsupported)"),
        ],
    )
    bucket_line = ", ".join(f"{k}: {v}" for k, v in buckets.items()) or "see conversion.json"
    return f"""# {title} — Code Migration Package

This package holds a **{src_p} → {tgt_p}** code conversion produced by Data Workbench
and is safe to browse, diff, and hand off. The target artifact kind is **{kind}**.

> The Workbench converts code the way it migrates data: it does **not** do a single-shot
> code→code translation. It **reverse-engineers** the legacy code into a reviewed
> use-case specification, then **forward-engineers** new code against the target platform's
> prescribed patterns and anti-patterns. Every converted file traces back through
> `codespec.json` → `design.md` → `new/`.

## Artifact inventory

{inv}

## What was converted

- **Original files:** {_md_bullets(old_files) if old_files else "_(see old/)_"}
- **Converted files:** {_md_bullets(new_files) if new_files else "_(see new/)_"}
- **Conversion outcome:** {bucket_line}

## How to review it

1. Read `codespec.json` for the intent the conversion was built against.
2. Diff `old/` against `new/` to see the mechanical change.
3. Check `conversion.json` for anything bucketed **manual_action** or **unsupported** —
   those need a human before the converted code is production-ready. A package with
   such items is a *converted-with-actions* result, not a clean success.

_This package is generated; do not hand-edit `old/` (it mirrors the imported source)._
"""


def assemble_code_migration_package(
    *, project_code: str,
    product_name: Optional[str] = None,
    readme_provider: Optional[Callable[[], Optional[str]]] = None,
) -> Path:
    """Assemble the downloadable code-migration package under
    ``<project>/serving/code_migration/``: ``old/`` (imported source), ``new/``
    (converted code), the reviewed ``codespec.json``, optional ``design.md``, the
    ``conversion.json`` report, and a README. Text-only; assembled on demand from
    the on-disk artifacts + the canonical DB spec."""
    import shutil
    from . import code_migration_orchestrator as cmo

    dest = package_dir(project_code, "code_migration")
    dest.mkdir(parents=True, exist_ok=True)

    def _copy_tree(src: Path, sub: str) -> list[str]:
        out: list[str] = []
        target = dest / sub
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        if src.is_dir():
            for p in sorted(src.rglob("*")):
                if p.is_file():
                    rel = p.relative_to(src)
                    d = target / rel
                    d.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        d.write_bytes(p.read_bytes())
                        out.append(str(rel))
                    except OSError:
                        pass
        return out

    old_files = _copy_tree(cmo.source_dir(project_code), "old")
    new_files = _copy_tree(cmo.target_dir(project_code), "new")

    # Canonical spec from disk mirror (falls back to empty).
    spec = cmo.load_spec_from_disk(project_code) or {}
    (dest / "codespec.json").write_text(json.dumps(spec, indent=2, sort_keys=True), encoding="utf-8")

    conv_path = cmo.conversion_path(project_code)
    buckets: dict[str, int] = {}
    if conv_path.exists():
        try:
            conv = json.loads(conv_path.read_text(encoding="utf-8"))
            (dest / "conversion.json").write_text(json.dumps(conv, indent=2), encoding="utf-8")
            for c in (conv.get("constructs") or conv.get("items") or []):
                if isinstance(c, dict):
                    st = c.get("status", "converted")
                    buckets[st] = buckets.get(st, 0) + 1
        except (ValueError, OSError):
            pass
    design_path = cmo.design_path(project_code)
    if design_path.exists():
        try:
            (dest / "design.md").write_text(design_path.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError:
            pass

    meta = {
        "source_platform": spec.get("source_platform", ""),
        "target_platform": spec.get("target_platform", ""),
        "artifact_kind": spec.get("artifact_kind", "sql_script"),
        "old_files": old_files, "new_files": new_files, "buckets": buckets,
    }
    _write_readme(dest, readme_provider,
                  lambda: _fallback_code_migration_readme(project_code, meta, product_name))
    return dest


# ── cross-platform transfer package ───────────────────────────────────────────

def _fallback_transfer_readme(project_code, spec, product_name=None) -> str:
    title = product_name or project_code
    sp = spec.get("source_platform", "mysql")
    tp = spec.get("target_platform", "databricks")
    schema = spec.get("target_schema", "public")
    catalog = spec.get("target_catalog", "")
    datasets = spec.get("datasets", [])
    dest = f"{catalog}.{schema}" if catalog else schema
    # Transform-placement strategy (the decision the engineer made in Configure
    # Transform Placement, carried on the spec and realized by the runner).
    placement = spec.get("placement", "transform_on_extract")
    requested = spec.get("requested_placement", placement)
    landing = spec.get("landing_relations", []) or []
    pl_warnings = spec.get("placement_warnings", []) or []
    n_elt = sum(1 for d in datasets if d.get("target_model_sql"))
    n_etl = len(datasets) - n_elt
    _PL_LABEL = {
        "transform_on_extract": "Transform on extract (ETL)",
        "hybrid": "Hybrid (EtLT)",
        "transfer_then_transform": "Transfer then transform (ELT)",
    }
    if placement == "transform_on_extract":
        strategy_body = (
            f"Every transform runs on the **{sp} source** as part of each dataset's "
            "`select_sql`, so column renames, masking, joins, and aggregations are "
            "already applied before any row is staged. Only shaped data crosses the "
            "boundary, and source↔target **row parity holds** (each source row maps to "
            "one target row), so reconciliation is a straight row-count compare."
        )
    else:
        strategy_body = (
            f"Cheap volume-reducers stay on the **{sp} source** while the heavy relational "
            f"work is deferred to the **{tp} target**. {len(landing)} base relation(s) are "
            f"landed into the `wb_landing` schema on the target, then {n_elt} dataset(s) are "
            "built as **post-load models** (`CREATE TABLE … AS …`) that read from the landed "
            f"relations; {n_etl} dataset(s) still shape on the source. Because transforms run "
            "in-flight, source↔target **row parity no longer holds** — reconciliation checks "
            "that each target model materialized (post-transform), not raw row equality."
        )
    if requested != placement:
        strategy_body += (
            f"\n\n> **Note:** the requested placement was *{_PL_LABEL.get(requested, requested)}* "
            f"but this package realizes *{_PL_LABEL.get(placement, placement)}*."
        )
    if pl_warnings:
        strategy_body += "\n\n" + "\n".join(f"> ⚠ {w}" for w in pl_warnings)
    strategy_section = (
        "## Transform placement strategy\n\n"
        f"**Strategy: {_PL_LABEL.get(placement, placement)}.** " + strategy_body + "\n\n"
    )
    inventory = _md_table(["File", "Purpose"], [
        ("`run.py`", "Entrypoint — the dlt transfer runner. **This is what you run.**"),
        ("`transfer.json`", "The transfer contract: per-dataset shaped SELECT + target table."),
        ("`_wb_runresult.py`", "Writes the structured `run_result.json` + `run.log`."),
        ("`requirements.txt`", "`dlt` + the target adapter + the source SQLAlchemy driver."),
        ("`.env.example`", "Template for the `WB_SOURCE_*` / `WB_TARGET_*` credentials — copy to `.env`."),
        ("`manifests/`", "One TransferBatch v1 manifest per dataset (created on load)."),
    ])
    # Summary table (identifiers only — safe in table cells) + full shaped SQL in
    # collapsible blocks below it. Handles ETL (select_sql), ELT/hybrid
    # (target_model_sql), and the missing-SQL case without shattering the GFM table.
    def _shaping_label(d) -> str:
        if d.get("target_model_sql"):
            return "Landed + modeled on target (ELT)"
        if d.get("select_sql"):
            return "Shaped on source (ETL)"
        return "—"
    ds_table = _md_table(["Target table", "Source relation", "Shaping"],
                         [(f"`{d.get('target_table','')}`",
                           f"`{d.get('physical_name') or '—'}`",
                           _shaping_label(d)) for d in datasets]) if datasets else ""
    ds_details = _md_sql_details([(d.get("target_table", ""),
                                   d.get("select_sql") or d.get("target_model_sql"))
                                  for d in datasets
                                  if d.get("select_sql") or d.get("target_model_sql")])
    ds_rows = ds_table + ("\n\n" + ds_details if ds_details else "") if ds_table else ""
    src_rows = _md_table(["Variable", "Required", "Description"], [
        ("`WB_SOURCE_PLATFORM`", "yes", f"Source platform (`{sp}`)."),
        ("`WB_SOURCE_DSN`", "yes*", "Full source DSN (or the discrete WB_SOURCE_HOST/PORT/USER/PASSWORD/DBNAME)."),
    ])
    tgt_rows = _md_table(["Variable", "Required", "Description"], _migration_target_env_rows(tp))
    # "How it works" is target-aware: a warehouse (snowflake/databricks) stages Parquet
    # then bulk-loads with COPY INTO via ITS OWN managed staging; a relational target
    # (postgres/mysql) is a direct bulk INSERT/COPY with no external stage. The old copy
    # asserted "stage Parquet → COPY INTO" for every target — false for relational ones.
    is_warehouse = tp.lower() in ("snowflake", "databricks")
    if is_warehouse:
        flow_load = (
            '  R -->|"stage Parquet"| STG["Parquet (staging)"]\n'
            f'  STG -->|"COPY INTO"| T[("{tp}<br/>{dest}")]\n'
        )
        load_prose = (
            f"dlt handles schema inference and the bulk load: for the **{tp}** warehouse it "
            "stages the shaped rows as Parquet in the warehouse's own managed staging area "
            "(a Unity Catalog volume / internal stage) and pulls them in with a single `COPY INTO`."
        )
    else:
        flow_load = f'  R -->|"bulk INSERT / COPY"| T[("{tp}<br/>{dest}")]\n'
        load_prose = (
            f"dlt handles schema inference and the load: for the relational **{tp}** target it "
            "writes the shaped rows directly (bulk `INSERT`/`COPY`) — there is no external staging step."
        )
    # Object storage is OPTIONAL and per-project (a ProjectArtifactStoreBinding), so the
    # README describes it conditionally rather than asserting it happened this run.
    object_store_note = (
        "\n\nWhen an **object store** is bound to the project, the same shaped Parquet also lands "
        "in it under a run-scoped prefix — browsable, with a run `manifest.json` and presigned "
        "download links. Against a warehouse-reachable cloud bucket the target can `COPY INTO` "
        "directly from those files (native staging); against a host-local fixture the store keeps a "
        "browsable copy while the load uses the target's own staging."
    )
    how_it_works = (
        "## How it works\n\n"
        "```mermaid\nflowchart LR\n"
        f'  SRC[("{sp} source")] -->|"shaped SELECT"| R["run.py (dlt)"]\n'
        '  ENV[".env → WB_SOURCE_* / WB_TARGET_*"] --> R\n'
        + flow_load +
        '  R --> OUT["run_result.json + run.log + manifests/"]\n'
        '  R -.->|"if object store bound"| OS[("object store<br/>s3://…")]\n```\n\n'
        + load_prose + object_store_note + "\n\n"
    )
    return (
        f"# {title} — cross-platform transfer serving package\n\n"
        f"A runnable **dlt** pipeline that serves this data product from its `{sp}` "
        f"source onto **{tp}** (`{dest}`), applying the product's transforms using the "
        f"**{_PL_LABEL.get(placement, placement)}** strategy (see below). This is the "
        f"same package Data Workbench runs to serve the product; download it and run it "
        f"yourself to reproduce the transfer.\n\n"
        "## Datasets transferred\n" + (ds_rows + "\n\n" if ds_rows else "") +
        "## What's in this package\n" + inventory + "\n\n"
        + how_it_works
        + strategy_section +
        "## Configuration (environment variables)\n\n"
        "Secrets ride the process env only — never written to disk. Copy "
        "`.env.example` → `.env` and fill in:\n\n"
        "**Source (read):**\n\n" + src_rows + "\n\n"
        "**Target (write):**\n\n" + tgt_rows + "\n\n"
        "## Usage\n\n"
        "```bash\npip install -r requirements.txt\ncp .env.example .env   # then edit .env\n"
        "set -a && . ./.env && set +a\n"
        "python run.py --mode load       # extract shaped rows → stage Parquet → load target\n"
        "python run.py --mode verify     # reconcile source-select vs target row counts\n"
        "python run.py --mode plan       # print what would run, touch nothing\n```\n\n"
        "## Outputs\n\n"
        "- `run_result.json` — machine-readable status + per-dataset results.\n"
        "- `run.log` — JSONL run log.\n"
        "- `manifests/*.json` — a TransferBatch v1 manifest per dataset.\n"
    )


def assemble_transfer_package(
    *, project_code: str, spec: dict,
    product_name: Optional[str] = None,
    readme_provider: Optional[Callable[[], Optional[str]]] = None,
) -> Path:
    """Assemble the runnable cross-platform transfer package under
    ``<project>/serving/transfer/``.

    ``spec`` is the transfer contract (``{source_platform, target_platform,
    target_catalog?, target_schema, write_disposition, datasets:[{target_table,
    select_sql, primary_key?}]}``) the backend compiled from the product's graph.
    Mirrors ``assemble_migration_package`` — dlt-based, no DuckDB — but each
    dataset carries a shaped ``select_sql`` rather than a raw source table.
    """
    dest = package_dir(project_code, "transfer")
    dest.mkdir(parents=True, exist_ok=True)
    spec_json = json.dumps(spec, indent=2)
    _invalidate_stale_readme(dest, regenerated=_artifact_changed(dest / "transfer.json", spec_json))
    (dest / "transfer.json").write_text(spec_json, encoding="utf-8")
    _copy_runner(dest, "run_transfer.py", "_wb_runresult.py")
    (dest / "requirements.txt").write_text(
        _migration_requirements(spec.get("source_platform", "mysql"),
                                spec.get("target_platform", "databricks")), encoding="utf-8")
    (dest / ".env.example").write_text(
        _migration_env_example(spec.get("source_platform", "mysql"),
                               spec.get("target_platform", "databricks")), encoding="utf-8")
    (dest / ".gitignore").write_text(_GITIGNORE, encoding="utf-8")
    _write_readme(dest, readme_provider,
                  lambda: _fallback_transfer_readme(project_code, spec, product_name))
    return dest


# ── dbt package ───────────────────────────────────────────────────────────────

_DBT_ADAPTERS = {
    "postgres": "dbt-postgres>=1.7,<2.0", "postgresql": "dbt-postgres>=1.7,<2.0",
    "mysql": "dbt-mysql>=1.7", "snowflake": "dbt-snowflake>=1.7,<2.0",
    "databricks": "dbt-databricks>=1.7,<2.0", "duckdb": "dbt-duckdb>=1.7,<2.0",
}

_DBT_BOOTSTRAP_PY = '''\
#!/usr/bin/env python3
"""Bootstrap + build this dbt project on a bare machine (stdlib only).
Creates .venv, installs requirements.txt, then runs `python run.py`.
Fill in .env (copied from .env.example) first — dbt reads WB_DBT_* from it."""
import os
import subprocess
import sys
import venv
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENV = HERE / ".venv"
BIN = VENV / ("Scripts" if os.name == "nt" else "bin")


def main():
    if not VENV.exists():
        print("creating .venv ...")
        venv.EnvBuilder(with_pip=True).create(str(VENV))
    py = str(BIN / ("python.exe" if os.name == "nt" else "python"))
    subprocess.run([py, "-m", "pip", "install", "-q", "-r", str(HERE / "requirements.txt")], check=True)
    # Load .env into the environment (WB_DBT_* creds) if present.
    env = dict(os.environ)
    dotenv = HERE / ".env"
    if dotenv.exists():
        for line in dotenv.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    subprocess.run([py, str(HERE / "run.py"), *sys.argv[1:]], env=env, check=False)


if __name__ == "__main__":
    main()
'''


def _dbt_env_example(platform: str) -> str:
    p = (platform or "postgres").lower()
    if p in ("postgres", "postgresql", "mysql"):
        return ("WB_DBT_HOST=localhost\nWB_DBT_PORT=5432\nWB_DBT_USER=\n"
                "WB_DBT_PASSWORD=\nWB_DBT_DBNAME=\n")
    if p == "snowflake":
        return ("WB_DBT_ACCOUNT=\nWB_DBT_USER=\nWB_DBT_PASSWORD=\n"
                "WB_DBT_DATABASE=\nWB_DBT_WAREHOUSE=\nWB_DBT_ROLE=\n")
    if p == "databricks":
        return "WB_DBT_HOST=\nWB_DBT_HTTP_PATH=\nWB_DBT_CATALOG=hive_metastore\nWB_DBT_TOKEN=\n"
    return "WB_DBT_HOST=localhost\nWB_DBT_USER=\nWB_DBT_PASSWORD=\nWB_DBT_DBNAME=\n"


def _dbt_env_rows(platform: str) -> list[tuple]:
    p = (platform or "postgres").lower()
    if p == "snowflake":
        return [
            ("`WB_DBT_ACCOUNT`", "yes", "Snowflake account identifier."),
            ("`WB_DBT_USER`", "yes", "Username."), ("`WB_DBT_PASSWORD`", "yes", "Password."),
            ("`WB_DBT_DATABASE`", "yes", "Database."), ("`WB_DBT_WAREHOUSE`", "yes", "Warehouse."),
            ("`WB_DBT_ROLE`", "no", "Role."),
        ]
    if p == "databricks":
        return [
            ("`WB_DBT_HOST`", "yes", "Workspace host."),
            ("`WB_DBT_HTTP_PATH`", "yes", "SQL warehouse HTTP path."),
            ("`WB_DBT_CATALOG`", "no", "Unity/Hive catalog (default `hive_metastore`)."),
            ("`WB_DBT_TOKEN`", "yes", "Personal access token."),
        ]
    return [
        ("`WB_DBT_HOST`", "yes", "Target host."), ("`WB_DBT_PORT`", "no", "Target port."),
        ("`WB_DBT_USER`", "yes", "Username."), ("`WB_DBT_PASSWORD`", "yes", "Password."),
        ("`WB_DBT_DBNAME`", "yes", "Database name."),
    ]


def _fallback_dbt_readme(project_code, platform, model_names, product_name=None,
                         materialization="table", has_snapshots=False) -> str:
    title = product_name or project_code
    model_list = _md_bullets([f"`{n}`" for n in model_names])
    inventory = _md_table(["File / dir", "Purpose"], [
        ("`run.py`", "Entrypoint. Runs `dbt build`. **This is what you run.**"),
        ("`bootstrap.py`", "Zero-setup helper: creates `.venv`, installs deps, loads `.env`, runs `run.py`."),
        ("`dbt_project.yml`", "dbt project config."),
        ("`profiles.yml`", "Connection profile — reads `WB_DBT_*` via `env_var()` (no secrets on disk)."),
        ("`models/`", "The SQL models (+ dbt **snapshots** for SCD2 history)."),
        ("`_wb_runresult.py`", "Writes the structured `run_result.json` + `run.log`."),
        ("`requirements.txt`", "`dbt-core` + the target adapter."),
        ("`.env.example`", "Template for the `WB_DBT_*` credentials — copy to `.env`."),
        ("`target/`", "dbt's compiled SQL + `run_results.json` (created on build; git-ignored)."),
    ])
    env_table = _md_table(["Variable", "Required", "Description"], _dbt_env_rows(platform))
    return (
        f"# {title} — dbt materialization serving package\n\n"
        f"A runnable **dbt** project that builds the product into physical tables "
        f"(and SCD2 snapshots) on a **{platform}** target. This is the same project "
        "Data Workbench runs to materialize the product.\n\n"
        "## Models built\n" + model_list + "\n\n"
        "## What's in this package\n" + inventory + "\n\n"
        "## How it works\n\n"
        "```mermaid\nflowchart LR\n"
        '  SRC[("source tables")] --> MOD["models/ (SQL)"]\n'
        '  ENV[".env → WB_DBT_*"] --> R["run.py → dbt build"]\n'
        '  MOD --> R\n'
        f'  R --> T[("{platform} target<br/>tables + snapshots")]\n'
        '  R --> OUT["run_result.json + run.log"]\n```\n\n'
        + _tech_mechanics_section("dbt", platform, materialization=materialization,
                                  has_snapshots=has_snapshots) + "\n"
        "## Configuration (environment variables)\n\n"
        "`profiles.yml` resolves these at build time via dbt's `env_var()` — "
        "credentials never touch disk.\n\n" + env_table + "\n\n"
        + _how_to_run_section() + "\n"
        "## Building the models\n\n"
        "**Option A — one command (recommended):**\n\n"
        "```bash\ncp .env.example .env      # then edit .env with your WB_DBT_* credentials\n"
        "python bootstrap.py       # creates .venv, installs deps, loads .env, runs dbt build\n```\n\n"
        "**Option B — you already have dbt + the adapter:**\n\n"
        "```bash\npip install -r requirements.txt\nset -a && . ./.env && set +a\n"
        "python run.py\n```\n\n"
        "**`run.py` flags**\n\n"
        + _md_table(["Flag", "Effect"], [
            ("`--mode full`", "Build every row (default)."),
            ("`--mode sample`", "Build a capped sample (fast smoke test)."),
            ("`--sample-limit N`", "Row cap for `--mode sample` (default 100)."),
            ("`--target NAME`", "dbt target to build against (default: prod for full, preview for sample)."),
            ("`--dbt-bin PATH`", "Path to the `dbt` executable (default `dbt`)."),
            ("`--timeout SECONDS`", "Build timeout (default 1800)."),
        ]) + "\n\n"
        "## Outputs\n\n"
        "- `run_result.json` — machine-readable status + per-model results.\n"
        "- `run.log` — JSONL run log.\n"
        "- `target/` — dbt's own compiled SQL and `run_results.json`.\n\n"
        "All three are regenerated every build and are git-ignored.\n"
    )


def assemble_dbt_package(
    dbt_dir: Path, *, project_code: str, platform: str, model_names: list[str],
    product_name: Optional[str] = None, materialization: str = "table",
    has_snapshots: bool = False,
    readme_provider: Optional[Callable[[], Optional[str]]] = None,
) -> Path:
    """Augment a scaffolded dbt project (``dbt_dir``) into a downloadable, runnable
    package: adds ``run.py`` + ``bootstrap.py`` + ``requirements.txt`` +
    ``.env.example`` + ``.gitignore`` + README alongside the dbt sources."""
    dbt_dir.mkdir(parents=True, exist_ok=True)
    # A full (authoring) build passes a readme_provider; sample-gate builds pass None
    # and rely on preserve-existing. Treat a full build as a regeneration so an
    # improved documenter/fallback reaches the package, then let _write_readme rewrite.
    _invalidate_stale_readme(dbt_dir, regenerated=(readme_provider is not None))
    _copy_runner(dbt_dir, "run_dbt.py", "_wb_runresult.py")
    (dbt_dir / "bootstrap.py").write_text(_DBT_BOOTSTRAP_PY, encoding="utf-8")
    adapter = _DBT_ADAPTERS.get((platform or "postgres").lower(), "dbt-postgres>=1.7,<2.0")
    (dbt_dir / "requirements.txt").write_text(
        f"dbt-core>=1.7,<2.0\n{adapter}\n", encoding="utf-8")
    (dbt_dir / ".env.example").write_text(_dbt_env_example(platform), encoding="utf-8")
    # Keep dbt's own ignores + our run artifacts.
    (dbt_dir / ".gitignore").write_text(
        "target/\ndbt_packages/\nlogs/\n.venv/\n.env\nrun_result.json\nrun.log\n",
        encoding="utf-8")
    _write_readme(dbt_dir, readme_provider,
                  lambda: _fallback_dbt_readme(project_code, platform, model_names,
                                               product_name, materialization, has_snapshots))
    return dbt_dir


# ── zip / collect helpers ─────────────────────────────────────────────────────

# Extensions treated as binary (returned as bytes, not str).
_BINARY_SUFFIXES = {".parquet", ".duckdb", ".db", ".zip", ".png", ".jpg", ".gz"}
# Runtime artifacts excluded from the download (per-run outputs, caches).
_EXCLUDE_NAMES = {"run_result.json", "run.log", ".env"}
_EXCLUDE_DIRS = {"__pycache__", "target", "logs", "dbt_packages", ".venv"}


def collect_package_files(package_root: Path) -> dict[str, object]:
    """``{relative_path: str|bytes}`` for every file in the package, excluding
    per-run artifacts and caches. Binary files (parquet/duckdb) as bytes."""
    out: dict[str, object] = {}
    if not package_root.exists():
        return out
    for p in sorted(package_root.rglob("*")):
        if not p.is_file():
            continue
        if any(part in _EXCLUDE_DIRS for part in p.relative_to(package_root).parts):
            continue
        if p.name in _EXCLUDE_NAMES:
            continue
        rel = str(p.relative_to(package_root))
        if p.suffix.lower() in _BINARY_SUFFIXES:
            out[rel] = p.read_bytes()
        else:
            try:
                out[rel] = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                out[rel] = p.read_bytes()
    return out


def to_zip_bytes(files: dict[str, object], root_prefix: str = "") -> bytes:
    """Zip a ``{path: str|bytes}`` map. ``root_prefix`` prepends a top dir."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel, content in files.items():
            arcname = f"{root_prefix}/{rel}" if root_prefix else rel
            if isinstance(content, bytes):
                zf.writestr(arcname, content)
            else:
                zf.writestr(arcname, str(content))
    return buf.getvalue()


def zip_response(files: dict[str, object], filename: str, root_prefix: str = "") -> Response:
    """A FastAPI ``Response`` streaming the zip as an attachment."""
    return Response(
        content=to_zip_bytes(files, root_prefix),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── git assembly ─────────────────────────────────────────────────────────────


def _collect_text_only(package_root: Path, prefix: str) -> dict[str, str]:
    """``collect_package_files`` filtered to text only (drops parquet/duckdb/etc.),
    with each path prefixed. A git repo carries code + docs, never data."""
    out: dict[str, str] = {}
    for rel, content in collect_package_files(package_root).items():
        if isinstance(content, str):
            out[f"{prefix}/{rel}" if prefix else rel] = content
    return out


# ── object-store publish collection (ADR-14) ───────────────────────────────────

# Explicit allowlist of PUBLISHABLE data-artifact directories: (rel_group, subdir
# under projects/<code>). We collect Parquet payloads + their paired TransferBatch
# manifests from these known producers ONLY — never a recursive upload. Excluded
# by construction: catalog.duckdb (embeds local file:// view paths), .dlt state,
# dq_tests_*/results, and stray .db/.zip files (ADR-14 D9).
_OBJECT_STORE_DATA_DIRS = (
    ("exports", "exports"),                       # data-export-parquet skill
    ("lakehouse", "serving/lakehouse/data"),      # run_lakehouse.py export
)


def collect_for_object_store(project_code: str) -> list[dict]:
    """Return the allowlisted binary data artifacts to publish for one project.

    ``[{local_path: Path, rel_path: str, content_type: str, size: int, kind: str}]``
    — each Parquet plus its paired ``<stem>__manifest.json`` (when present).
    Best-effort: a missing producer dir just contributes nothing. The run-prefix
    protocol (object_store_publish.py) prepends ``<project_prefix>/runs/<run_id>/``.
    """
    base = BASE_PROJECT_DIR / project_code
    out: list[dict] = []
    for group, subdir in _OBJECT_STORE_DATA_DIRS:
        d = base / subdir
        if not d.is_dir():
            continue
        for pq in sorted(d.glob("*.parquet")):
            out.append({
                "local_path": pq, "rel_path": f"{group}/{pq.name}",
                "content_type": "application/octet-stream",
                "size": pq.stat().st_size, "kind": "parquet",
            })
            manifest = pq.with_name(f"{pq.stem}__manifest.json")
            if manifest.is_file():
                out.append({
                    "local_path": manifest, "rel_path": f"{group}/{manifest.name}",
                    "content_type": "application/json",
                    "size": manifest.stat().st_size, "kind": "manifest",
                })
    return out


def collect_for_git(project, settings) -> dict[str, str]:
    """Assemble every git-pushable file for one data product: the serving
    package(s) (view / dbt / migration / transfer / lakehouse code), the OKF docs
    (``product.md`` → ``README.md`` + ``docs/datasets/``), and the ODCS spec as
    YAML. Binary data (parquet/duckdb) is intentionally excluded — repos hold
    artifacts, not data.

    Best-effort per source: a missing package or an unreachable graph just
    contributes nothing rather than raising.
    """
    project_code = project.project_code
    files: dict[str, str] = {}

    # 1. Serving artifacts. The primary serving package — a virtual-view package
    #    OR a dbt project (mutually exclusive, one per product) — lands at the REPO
    #    ROOT so the repo is clone-and-run: `view.sql` + `run.py` (view) or
    #    `dbt_project.yml` + `models/` (dbt) sit at top level. A lakehouse export
    #    (a secondary data-plane artifact) stays namespaced under `lakehouse/` so
    #    its runner files can't collide with the primary package's.
    #    A data-migration (dmig) project has no view/dbt primary — its migration
    #    runner package IS the repo, so it lands at the repo ROOT (third fallback).
    #    A cross-platform transfer product (serving mode transfer_then_transform)
    #    likewise has no view/dbt/migration package — its dlt transfer runner IS
    #    the repo, so it lands at the repo ROOT (fourth fallback). Text/code only;
    #    the runner excludes parquet/duckdb + per-run artifacts (Parquet is
    #    produced at run time in the target) — repos hold artifacts, not data.
    #    A code-migration (cmig) project has no view/dbt/migration/transfer
    #    package — its old/ + new/ + conversion package IS the repo, so it lands
    #    at the repo ROOT (fifth fallback). Text/code only.
    primary = _collect_text_only(package_dir(project_code, "view"), "") \
        or _collect_text_only(BASE_PROJECT_DIR / project_code / "dbt", "") \
        or _collect_text_only(package_dir(project_code, "migration"), "") \
        or _collect_text_only(package_dir(project_code, "transfer"), "") \
        or _collect_text_only(package_dir(project_code, "code_migration"), "")
    files.update(primary)
    files.update(_collect_text_only(package_dir(project_code, "lakehouse"), "lakehouse"))

    # 2. OKF docs. The serving package already ships the repo's landing README (its
    #    runnable six-section doc), so keep that as the root README and route the
    #    OKF product overview to docs/product.md. Only when no serving package
    #    exists does the OKF overview become the landing README.
    try:
        from .okf_export import collect_okf_files
        okf = collect_okf_files(settings, uri=f"dprod:{project_code}-contract")
        product_md = okf.get("product.md")
        if product_md:
            files["docs/product.md" if "README.md" in files else "README.md"] = product_md
        for key, val in okf.items():
            if key.startswith("datasets/"):
                files[f"docs/{key}"] = val
    except Exception:
        pass

    # 3. ODCS spec as YAML (the contract, human + machine readable).
    try:
        import yaml
        from .routers.odcs import _read_odcs_from_graph
        spec = _read_odcs_from_graph(f"{project_code}-contract", project)
        if spec:
            files["odcs.yaml"] = yaml.dump(
                spec, default_flow_style=False, sort_keys=False, allow_unicode=True)
    except Exception:
        pass

    # 4. DQ test code (every generated suite, excluding results/). An SA product
    # can hold both a catalog and a dprod suite, and a CF product only a dprod one,
    # so enumerate all present suites and preserve each in its own `_dprod`-aware
    # subdir. Disk detection → no session needed (mirrors the download path).
    try:
        from .dq_package import assemble_dq_package, available_dq_suites
        from .dq_test_executor import _FRAMEWORKS as _DQ_FW, dq_subdir as _dq_subdir
        for suite in available_dq_suites(project_code):
            fw_meta = next((f for f in _DQ_FW if f["framework"] == suite["framework"]), None)
            if not fw_meta:
                continue
            sub = _dq_subdir(fw_meta["subdir"], suite["source_mode"])
            # assemble_dq_package = the test files PLUS the README (+ dprod note) —
            # collect_dq_files alone omits the README, which is why the pushed
            # subfolder was missing it while the Download package had it.
            pkg = assemble_dq_package(project_code, suite["framework"], suite["source_mode"])
            for rel, text in (pkg or {}).items():
                files[f"{sub}/{rel}"] = text
    except Exception:
        pass

    return files
