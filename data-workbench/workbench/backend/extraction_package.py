"""Extraction-kit assembly — the client-run offline extractor as a downloadable ZIP.

Sibling of ``serving_package.py`` (reuses its zip/collect utilities verbatim). An
extraction kit is a tailored, self-contained directory a CLIENT downloads and runs
in THEIR environment to produce a reviewable ``estate-manifest-<ts>.yaml``:

  * the stdlib-only runner (``run.py`` + ``_extract_core`` / ``_manifest_writer`` /
    ``_wb_runresult``, copied from ``extraction_runners/`` + ``serving_runners/``),
  * a per-platform ``requirements.txt`` (the ONE DB driver + ``pyyaml`` + the
    interactive-picker lib),
  * ``manifest_config.json`` (redaction caps + PII token list, baked in),
  * ``.env.example`` (the ``WB_SOURCE_*`` blanks — the one thing DW never has),
  * ``.gitignore`` and a rich README (two Mermaid diagrams + env/flag tables).

NO ``workbench.*`` at runtime, NO LLM inside the tool — deterministic extraction
only. The README follows the repo's seven-section package-README contract.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Callable, Optional

from .config import BASE_PROJECT_DIR
from .estate import _PII_TOKENS
from .estate_manifest import MANIFEST_SCHEMA_VERSION
from .serving_package import (
    _GITIGNORE,
    _how_to_run_section,
    _md_bullets,
    _md_table,
    _write_readme,
    collect_package_files,
    zip_response,
)
from .serving_runtime import SERVING_RUNNERS_DIR

EXTRACTION_RUNNERS_DIR = Path(__file__).resolve().parent / "extraction_runners"

# Per-platform driver requirement (the ONE third-party DB dep). Everything else is
# stdlib + pyyaml + the picker lib.
_EXTRACTION_DRIVER_REQUIREMENTS = {
    "postgres": ["psycopg2-binary>=2.9,<3.0"],
    "postgresql": ["psycopg2-binary>=2.9,<3.0"],
    "mysql": ["pymysql>=1.0,<2.0"],
    "snowflake": ["snowflake-connector-python>=3.0.0"],
    "databricks": ["databricks-sql-connector>=3.0.0"],
    "duckdb": ["duckdb>=0.10"],
}

# The interactive picker (questionary → prompt_toolkit → wcwidth) + pyyaml. A small,
# pure-Python, well-established set; bypassed via --all/--selection when there's no TTY.
_PICKER_REQUIREMENTS = ["pyyaml>=6.0", "questionary>=2.0"]


def extraction_package_dir(source_id: int) -> Path:
    """``projects/_estate_extraction/<source_id>/`` — the on-disk build dir."""
    return BASE_PROJECT_DIR / "_estate_extraction" / str(source_id)


def _default_manifest_config() -> dict:
    return {
        "manifest_version": MANIFEST_SCHEMA_VERSION,
        "catalog": None,                 # null → the tool derives from WB_SOURCE_*
        "profiling_default": True,
        "volumetrics_default": True,
        "include_values_default": True,
        "sample_limit": 100000,
        "top_n": 10,
        "max_enum_cardinality": 50,
        "max_value_length": 200,
        "pii_tokens": list(_PII_TOKENS),
    }


# ── .env.example (per-platform WB_SOURCE_* blanks) ────────────────────────────

def _extraction_env_example(platform: str) -> str:
    p = (platform or "postgres").lower()
    header = ("# Data Workbench NEVER sees these — they live only here / in your shell.\n"
              "# Read-only introspection credentials for the OFFLINE extractor.\n\n")
    if p in ("postgres", "postgresql"):
        return header + ("WB_SOURCE_PLATFORM=postgres\n"
                         "WB_SOURCE_DSN=postgresql://user:password@host:5432/dbname\n"
                         "# …or discrete parts (DSN wins if both set):\n"
                         "# WB_SOURCE_HOST=localhost\n# WB_SOURCE_PORT=5432\n"
                         "# WB_SOURCE_USER=\n# WB_SOURCE_PASSWORD=\n# WB_SOURCE_DBNAME=\n")
    if p == "mysql":
        return header + ("WB_SOURCE_PLATFORM=mysql\nWB_SOURCE_HOST=localhost\nWB_SOURCE_PORT=3306\n"
                         "WB_SOURCE_USER=\nWB_SOURCE_PASSWORD=\nWB_SOURCE_DBNAME=\n")
    if p == "snowflake":
        return header + ("WB_SOURCE_PLATFORM=snowflake\n"
                         "WB_SOURCE_ACCOUNT=   # account identifier (falls back to WB_SOURCE_HOST)\n"
                         "WB_SOURCE_USER=\n"
                         "WB_SOURCE_PASSWORD=  # a Snowflake PAT authenticates as a plain password\n"
                         "WB_SOURCE_DBNAME=\nWB_SOURCE_WAREHOUSE=\nWB_SOURCE_ROLE=\n")
    if p == "databricks":
        return header + ("WB_SOURCE_PLATFORM=databricks\nWB_SOURCE_HOST=\nWB_SOURCE_HTTP_PATH=\n"
                         "WB_SOURCE_CATALOG=\nWB_SOURCE_TOKEN=\n")
    if p == "duckdb":
        return header + ("WB_SOURCE_PLATFORM=duckdb\nWB_SOURCE_DSN=/path/to/database.duckdb\n")
    return header + f"WB_SOURCE_PLATFORM={p}\n"


def _extraction_env_rows(platform: str) -> list[tuple]:
    p = (platform or "postgres").lower()
    rows = [("`WB_SOURCE_PLATFORM`", "yes", f"Source engine (`{p}`).")]
    if p in ("postgres", "postgresql"):
        rows += [
            ("`WB_SOURCE_DSN`", "one of", "Full DSN `postgresql://user:pw@host:5432/db` (wins if set)."),
            ("`WB_SOURCE_HOST`", "or", "Host (when not using a DSN)."),
            ("`WB_SOURCE_PORT`", "no", "Port (default `5432`)."),
            ("`WB_SOURCE_USER`", "yes*", "Read-only username."),
            ("`WB_SOURCE_PASSWORD`", "yes*", "Password."),
            ("`WB_SOURCE_DBNAME`", "yes*", "Database to introspect."),
        ]
    elif p == "mysql":
        rows += [
            ("`WB_SOURCE_HOST` / `WB_SOURCE_PORT`", "yes", "Host + port (default 3306)."),
            ("`WB_SOURCE_USER` / `WB_SOURCE_PASSWORD`", "yes", "Read-only credentials."),
            ("`WB_SOURCE_DBNAME`", "yes", "Database to introspect."),
        ]
    elif p == "snowflake":
        rows += [
            ("`WB_SOURCE_ACCOUNT`", "yes", "Account identifier (or `WB_SOURCE_HOST`)."),
            ("`WB_SOURCE_USER` / `WB_SOURCE_PASSWORD`", "yes", "Credentials (a PAT works as the password)."),
            ("`WB_SOURCE_DBNAME`", "yes", "Database (the manifest `catalog`)."),
            ("`WB_SOURCE_WAREHOUSE`", "yes", "Warehouse for the read queries."),
            ("`WB_SOURCE_ROLE`", "no", "Role."),
        ]
    elif p == "databricks":
        rows += [
            ("`WB_SOURCE_HOST`", "yes", "Workspace hostname."),
            ("`WB_SOURCE_HTTP_PATH`", "yes", "SQL warehouse HTTP path."),
            ("`WB_SOURCE_CATALOG`", "yes", "Unity Catalog (the manifest `catalog`)."),
            ("`WB_SOURCE_TOKEN`", "yes", "Personal access token."),
        ]
    return rows


# ── README (fallback with the two required Mermaid diagrams) ──────────────────

def _fallback_extraction_readme(platform: str, source_name: str = "",
                                catalog: str = "") -> str:
    title = source_name or f"{platform} estate source"
    inventory = _md_table(["File", "Purpose"], [
        ("`run.py`", "Entrypoint. Connects, runs the interactive picker, extracts. **This is what you run.**"),
        ("`_extract_core.py`", "Read-only introspection + guarded profiling per platform (no full-table dumps)."),
        ("`_manifest_writer.py`", "Assembles the manifest + applies the PII/cardinality/length redaction."),
        ("`_wb_runresult.py`", "Writes the structured `extract_result-<ts>.json` + `run.log`."),
        ("`manifest_config.json`", "Baked-in redaction caps + PII token list + defaults."),
        ("`requirements.txt`", "Python deps: the one DB driver, `pyyaml`, and the picker lib."),
        ("`.env.example`", "Template for the `WB_SOURCE_*` connection env vars — copy to `.env`."),
        ("`.gitignore`", "Excludes `.env` + per-run artifacts."),
        ("`estate-manifest-<ts>.yaml`", "**The output** — the reviewable metadata you send back."),
        ("`selection-<ts>.yaml`", "A replayable record of the exact schemas/tables you picked."),
    ])
    env_table = _md_table(["Variable", "Required", "Description"], _extraction_env_rows(platform))
    flag_table = _md_table(["Flag", "Effect"], [
        ("`--all`", "Non-interactive: every schema + table (CI / demo / no-TTY)."),
        ("`--selection selection-<ts>.yaml`", "Replay a prior pick headlessly."),
        ("`--no-profile`", "Metadata + volumetrics only (skip all profiling)."),
        ("`--no-values`", "Drop every value-bearing field globally (counts only)."),
        ("`--no-volumetrics`", "Skip row counts / size / last-modified."),
        ("`--sample-limit N`", "Max rows sampled per table for profiling (default 100000)."),
        ("`--top-n N`", "Top frequent values per low-card column (default 10)."),
        ("`--max-enum-cardinality K`", "Above K distinct values → counts only, no enumeration (default 50)."),
        ("`--exclude-column schema.table.col`", "Skip profiling for a specific column (repeatable)."),
        ("`--code-assets`", "Also capture code assets where supported."),
    ])
    pg_footnote = (
        "`*` For Postgres, supply **either** `WB_SOURCE_DSN` **or** the discrete "
        "`WB_SOURCE_HOST/PORT/USER/PASSWORD/DBNAME` parts (the DSN wins).\n\n"
        if (platform or "postgres").lower() in ("postgres", "postgresql") else "")
    catalog_line = (f" for catalog **`{catalog}`**" if catalog else "")
    return (
        f"# {title} — offline metadata extraction kit\n\n"
        f"A small, **client-run** tool that introspects your **{platform}** platform"
        f"{catalog_line} and writes a **reviewable** `estate-manifest-<ts>.yaml` for "
        "Data Workbench. It has **no dependency on Data Workbench at runtime, no "
        "LLM/agent inside, is read-only, and you review the YAML before you send it.** "
        "Your credentials never leave your environment.\n\n"
        "## What's in this kit\n" + inventory + "\n\n"
        "## How it works\n\n"
        "```mermaid\nflowchart LR\n"
        '  env[".env / WB_SOURCE_*<br/>(your credentials)"] --> run["run.py extract"]\n'
        '  run --> pick["interactive picker<br/>schemas → tables<br/>(select-all / all-in-schema)"]\n'
        '  pick --> introspect["read-only introspection<br/>+ guarded profiling"]\n'
        '  introspect --> out["estate-manifest-&lt;ts&gt;.yaml<br/>+ extract_result / selection / run.log"]\n'
        '  out --> review["you review the YAML"] --> upload["upload to Data Workbench"]\n'
        "```\n\n"
        "## How the technology works\n\n"
        "The tool runs **read-only** catalog queries per platform — Postgres/MySQL/"
        "Snowflake/Databricks `information_schema` (plus `SHOW`/`DESCRIBE` for "
        "volumetrics) — to capture schemas, tables, columns, types, keys, foreign "
        "keys, comments, row counts, and sizes. Profiling runs **one bounded "
        "aggregate pass** per column (with sampling and an exact MIN/MAX override), "
        "never a full-table dump. A **PII name heuristic** plus cardinality/length "
        "caps then **null the value-bearing outputs** (`min`/`max`/`top_values`) for "
        "sensitive or high-cardinality columns, keeping only counts — and every "
        "withholding is tallied in the manifest's `redaction` summary.\n\n"
        "```mermaid\nflowchart TD\n"
        '  q["read-only information_schema / SHOW / DESCRIBE"] --> meta["schemas · tables · columns<br/>types · PK/FK · comments · volumetrics"]\n'
        '  meta --> prof["guarded profiling<br/>(bounded sample, exact MIN/MAX)"]\n'
        '  prof --> guard{"PII name? over cardinality/length cap?"}\n'
        '  guard -- yes --> red["null min/max/top_values<br/>keep counts · record redaction"]\n'
        '  guard -- no --> keep["keep safe enumerations"]\n'
        '  red --> yaml["estate-manifest-&lt;ts&gt;.yaml"]\n  keep --> yaml\n'
        "```\n\n"
        "## Configuration (environment variables)\n\n" + env_table + "\n\n"
        + pg_footnote
        + _how_to_run_section() + "\n"
        "## Usage\n\n"
        "```bash\npython -m venv .venv && . .venv/bin/activate   # Windows: .venv\\Scripts\\activate\n"
        "pip install -r requirements.txt\n"
        "cp .env.example .env            # then edit .env with your read-only credentials\n"
        "set -a && . ./.env && set +a    # load the connection into the environment\n"
        "python run.py extract           # interactive picker: choose schemas → tables\n"
        "# non-interactive escapes:\n"
        "python run.py extract --all                          # every schema + table\n"
        "python run.py extract --selection selection-<ts>.yaml # replay a prior pick\n"
        "python run.py list                                   # just print the inventory\n```\n\n"
        + flag_table + "\n\n"
        "## Outputs\n\n"
        "- `estate-manifest-<ts>.yaml` — **the file you send back.** Human-readable by "
        "design: schema/table/column names, types, counts, and only *safe* enumerated "
        "values. Open it and confirm nothing sensitive is present — the `redaction` "
        "block shows what was withheld.\n"
        "- `extract_result-<ts>.json` — machine-readable run status + metrics.\n"
        "- `selection-<ts>.yaml` — the exact schemas/tables you picked (replayable).\n"
        "- `run.log` — line-by-line JSONL log.\n\n"
        "Re-runs are timestamped, so a full before/after history is kept.\n"
    )


def assemble_extraction_package(
    platform: str, *, source_id: int, source_name: str = "", catalog: str = "",
    config_overrides: Optional[dict] = None,
    readme_provider: Optional[Callable[[], Optional[str]]] = None,
) -> Path:
    """Assemble the tailored extraction kit under
    ``projects/_estate_extraction/<source_id>/`` and return the dir."""
    dest = extraction_package_dir(source_id)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)

    cfg = _default_manifest_config()
    if catalog:
        cfg["catalog"] = catalog
    if config_overrides:
        cfg.update(config_overrides)
    (dest / "manifest_config.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    # Runner scripts (extraction_runners/) + the shared result recorder (serving_runners/).
    shutil.copyfile(EXTRACTION_RUNNERS_DIR / "run_extract.py", dest / "run.py")
    for h in ("_extract_core.py", "_manifest_writer.py"):
        shutil.copyfile(EXTRACTION_RUNNERS_DIR / h, dest / h)
    shutil.copyfile(SERVING_RUNNERS_DIR / "_wb_runresult.py", dest / "_wb_runresult.py")

    reqs = _EXTRACTION_DRIVER_REQUIREMENTS.get((platform or "postgres").lower(), []) \
        + _PICKER_REQUIREMENTS
    (dest / "requirements.txt").write_text("\n".join(reqs) + "\n", encoding="utf-8")
    (dest / ".env.example").write_text(_extraction_env_example(platform), encoding="utf-8")
    (dest / ".gitignore").write_text(
        _GITIGNORE + "estate-manifest-*.yaml\nextract_result-*.json\nselection-*.yaml\n",
        encoding="utf-8")
    _write_readme(dest, readme_provider,
                  lambda: _fallback_extraction_readme(platform, source_name, catalog))
    return dest


def extraction_package_files(source_id: int, platform: str, *, source_name: str = "",
                             catalog: str = "") -> dict[str, object]:
    """Assemble (if needed) + collect the kit's files for a JSON/zip download."""
    dest = assemble_extraction_package(
        platform, source_id=source_id, source_name=source_name, catalog=catalog)
    return collect_package_files(dest)


def extraction_zip_response(source_id: int, platform: str, *, source_name: str = "",
                            catalog: str = "", filename: Optional[str] = None):
    files = extraction_package_files(source_id, platform, source_name=source_name, catalog=catalog)
    fname = filename or f"extraction-kit-{platform}-{source_id}.zip"
    return zip_response(files, fname, root_prefix=f"extraction-kit-{source_id}")
