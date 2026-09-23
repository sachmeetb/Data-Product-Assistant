"""
pipeline_generator.py — Pipeline Generator Agent.

Translates STTM, data contracts, and silver/gold table specs into an
executable multi-statement Databricks SQL script.
"""

import asyncio
import json
import os
import re

from .base import run_agent, _PROJECT_ROOT
from dotenv import load_dotenv
from tools.schema_inspector import inspect_tables, extract_target_tables

load_dotenv(_PROJECT_ROOT / ".env")


# Path to the authoritative data catalog. The pipeline-generator MUST source
# every column name, data type, and fully-qualified table name from this file
# rather than fabricating them from the spec or its own memory.
_UTILITY_CATALOG_PATH = _PROJECT_ROOT / "data" / "utility_catalog.json"


def _load_utility_catalog() -> dict:
    """Load utility_catalog.json fresh on every pipeline-generator call.

    Reading on each call (instead of caching) means any edits the user makes
    to the catalog (manually or via DDI updates) take effect immediately on
    the very next generation — no server restart required.

    Returns an empty dict on any read/parse failure so the agent still runs;
    the SKILL.md treats a missing catalog as a fatal upstream error.
    """
    try:
        with open(_UTILITY_CATALOG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[PG] WARN: could not load utility_catalog.json: {exc!r}")
        return {}


# Catalog names that the static utility_catalog.json / DDI artefacts hardcode
# in fully-qualified table names. When DATABRICKS_CATALOG is set in the env we
# rewrite every FQDN in the spec so the SQL targets the user's actual workspace
# catalog (e.g. `program_one_dev`) rather than the design-time `acn_consumption`
# / `acn_aggregated` placeholders, which don't exist in the workspace and cause
# the publisher to fail with NO_SUCH_CATALOG_EXCEPTION.
_REWRITABLE_CATALOGS = ("acn_consumption", "acn_aggregated", "acn_source")


def _rewrite_catalog_prefixes(spec: dict, env_catalog: str) -> dict:
    """Replace design-time catalog prefixes in any string field of spec with env_catalog."""
    if not env_catalog or env_catalog in _REWRITABLE_CATALOGS:
        return spec
    try:
        as_json = json.dumps(spec)
    except (TypeError, ValueError):
        return spec
    for old in _REWRITABLE_CATALOGS:
        as_json = as_json.replace(f"{old}.", f"{env_catalog}.")
    try:
        return json.loads(as_json)
    except (TypeError, ValueError):
        return spec


async def run(spec: dict, test_feedback: dict | None = None, session=None) -> dict:
    """
    Generate a Databricks SQL pipeline script from the provided specification.

    The catalog and environment are resolved in priority order:
      1. DATABRICKS_CATALOG env var (wins so the SQL targets the user's actual
         workspace; design-time prefixes like `acn_consumption.X.Y` get rewritten
         to `${DATABRICKS_CATALOG}.X.Y`)
      2. spec['catalog'] / spec['target_environment']
      3. Defaults: 'main' / 'dev'

    Args:
        spec:          Pipeline specification dict.
        test_feedback: Failure details from Test Agent on a previous iteration.

    Returns:
        Dict with keys:
          generated_code  — str, the complete multi-statement SQL script
          pipeline_type   — "sql"
          target_tables   — list of Unity Catalog table paths
          statement_count — number of SQL statements
          display_output  — human-readable generation summary (optional)
    """
    env_catalog = (os.environ.get("DATABRICKS_CATALOG") or "").strip()
    grant_principal = os.environ.get("DATABRICKS_GRANT_PRINCIPAL", "").strip()
    enriched_spec = {
        **spec,
        "catalog": env_catalog or spec.get("catalog") or "main",
        "target_environment": spec.get("target_environment") or os.environ.get("TARGET_ENVIRONMENT", "dev"),
    }
    if env_catalog:
        # Rewrite every `acn_consumption.X.Y` / `acn_aggregated.X.Y` FQDN baked
        # into target_tables, source_tables, gold_schema, sttm, etc.
        enriched_spec = _rewrite_catalog_prefixes(enriched_spec, env_catalog)
    if grant_principal:
        enriched_spec["grant_principal"] = grant_principal

    context: dict = {"spec": enriched_spec}

    # Inject the authoritative data catalog so the agent generates SQL using
    # ONLY the column names, types, and fully-qualified table paths that
    # actually exist in utility_catalog.json. The SKILL.md treats this block
    # as the source of truth — fabricating column/table identifiers is forbidden.
    utility_catalog = _load_utility_catalog()
    if utility_catalog:
        context["utility_catalog"] = utility_catalog

    # Pre-flight schema inspection — ALWAYS run it, including on retries.
    # On a user_correction retry the agent may need to ALTER existing tables
    # (e.g. ADD/DROP COLUMN), so it must know what columns are live right now.
    target_tables = extract_target_tables(enriched_spec)
    if target_tables:
        existing = await asyncio.to_thread(inspect_tables, target_tables)
        if any(t["exists"] for t in existing.values()):
            context["existing_tables"] = existing

    # Distinguish three retry shapes:
    #   - feedback["user_correction"] present → user-driven regenerate (apply correction)
    #   - feedback["failures"]         present → test-agent retry (fix listed failures)
    #   - neither / no feedback        → first-pass generation
    user_correction = (test_feedback or {}).get("user_correction") if test_feedback else None
    has_test_failures = bool((test_feedback or {}).get("failures")) if test_feedback else False

    if user_correction:
        context["test_feedback"] = test_feedback
        prompt = (
            "═══════════════════════════════════════════════════════════════════\n"
            "  USER CORRECTION — ABSOLUTE TOP PRIORITY. APPLY LITERALLY.\n"
            "═══════════════════════════════════════════════════════════════════\n\n"
            f"The user said: {user_correction!r}\n\n"
            "RULES (in this order):\n"
            "  1. Parse the correction. Identify every concrete change:\n"
            "     • column add / remove / rename\n"
            "     • table add / remove\n"
            "     • transformation, join, filter, aggregation change\n"
            "  2. For EACH change on a table that already exists in\n"
            "     context.existing_tables, you MUST emit the corresponding\n"
            "     DDL statement up front. NO EXCEPTIONS — the \"skip DDL when\n"
            "     columns match\" rule (3a in your skill) is OVERRIDDEN by the\n"
            "     user_correction.\n"
            "       - remove → ALTER TABLE <fqn> DROP COLUMN <name>;\n"
            "       - add    → ALTER TABLE <fqn> ADD COLUMN <name> <type>;\n"
            "       - rename → ALTER TABLE <fqn> RENAME COLUMN <old> TO <new>;\n"
            "  3. Update every MERGE / SELECT / INSERT to match the new schema\n"
            "     (strip removed columns, add inserted columns, etc.)\n"
            "  4. Self-verify BEFORE returning: scan your generated_code text\n"
            "     for the column/table names the user mentioned. If those names\n"
            "     do not appear in an appropriate ALTER TABLE statement, you\n"
            "     have failed — REWRITE until they do.\n\n"
            "FORBIDDEN OUTPUTS:\n"
            "  • Returning a script identical to the previous attempt.\n"
            "  • A script with '0 tables, 0 quality constraints' on a correction\n"
            "    that asks for a schema change. (Schema changes REQUIRE ALTER\n"
            "    TABLE statements; that is the whole point of the user_correction.)\n"
            "  • Silently skipping the change because the live schema matches\n"
            "    the spec — the user is asking to CHANGE the live schema.\n\n"
            "Now generate the FULL, complete, multi-statement Databricks SQL\n"
            "script with the user's correction applied. Every prior statement\n"
            "that wasn't affected by the correction should still appear; the\n"
            "publisher executes the whole script, not a diff."
        )
    elif has_test_failures:
        context["test_feedback"] = test_feedback
        prompt = (
            "The previous SQL pipeline script failed validation. "
            "Review every failure in test_feedback, fix each one, "
            "and return the complete corrected SQL script."
        )
    else:
        prompt = (
            "Generate a production-grade Databricks SQL pipeline script "
            "for the specification provided in context."
        )

    result = await run_agent("pipeline-generator", prompt, context=context, session=session)

    # Defense in depth: even after the spec is rewritten, the LLM can still
    # emit the design-time catalog prefix verbatim (it's sticky in its
    # examples / memory). Rewrite the generated SQL text directly so the
    # publisher never sees `acn_consumption.X.Y` at execution time.
    if env_catalog and isinstance(result, dict):
        result = _rewrite_result_catalogs(result, env_catalog)

    # Deterministic safety net: replace every INSERT INTO/OVERWRITE VALUES
    # statement for a catalogged table with one whose rows come straight from
    # utility_catalog.sample_data. The LLM cannot smuggle fabricated rows into
    # the published tables anymore — Python rewrites the SQL before it reaches
    # the publisher.
    if utility_catalog and isinstance(result, dict):
        gen = result.get("generated_code")
        if isinstance(gen, str) and gen:
            new_gen = _enforce_catalog_seed_inserts(gen, utility_catalog)
            if new_gen != gen:
                result["generated_code"] = new_gen
                result["_seed_inserts_enforced"] = True

    return result


def _rewrite_str_catalog(s: str, env_catalog: str) -> str:
    for old in _REWRITABLE_CATALOGS:
        if old != env_catalog:
            s = s.replace(f"{old}.", f"{env_catalog}.")
    return s


def _rewrite_result_catalogs(result: dict, env_catalog: str) -> dict:
    """Rewrite design-time catalog prefixes in the pipeline-generator output."""
    gen = result.get("generated_code")
    if isinstance(gen, str) and gen:
        result["generated_code"] = _rewrite_str_catalog(gen, env_catalog)
    tt = result.get("target_tables")
    if isinstance(tt, list):
        result["target_tables"] = [
            _rewrite_str_catalog(t, env_catalog) if isinstance(t, str) else t
            for t in tt
        ]
    return result


# ── Catalog seed enforcement ──────────────────────────────────────────────────
# The LLM sometimes invents row data in `INSERT INTO/OVERWRITE <table> VALUES (...)`
# statements. Those statements get executed by the Publisher and the fabricated
# rows physically land in Databricks. The functions below run AFTER the LLM
# returns and rewrite every VALUES clause for a catalogged table so its row
# values come from `utility_catalog.json.sample_data` verbatim.

def _build_catalog_seed_index(catalog: dict) -> dict:
    """Build { full_name: { columns: [name,...], sample_data: [row_dict,...] } }
    from utility_catalog.json. Empty when the catalog is missing/malformed."""
    if not isinstance(catalog, dict):
        return {}
    data_catalog = catalog.get("data_catalog") or {}
    layers = (data_catalog.get("layers") or {})
    index: dict = {}
    for layer_name in ("bronze", "silver", "gold"):
        for entry in layers.get(layer_name) or []:
            if not isinstance(entry, dict):
                continue
            full_name = entry.get("full_name")
            if not full_name:
                continue
            cols = entry.get("columns") or []
            col_names = [c.get("name") for c in cols if isinstance(c, dict) and c.get("name")]
            sample_data = [r for r in (entry.get("sample_data") or []) if isinstance(r, dict)]
            index[full_name] = {"columns": col_names, "sample_data": sample_data}
    return index


def _sql_literal(value) -> str:
    """Render a Python value as a SQL literal for INSERT VALUES."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    # Strings (including dates/timestamps shipped as ISO strings) — wrap and escape '
    return "'" + str(value).replace("'", "''") + "'"


def _build_insert_from_catalog(target_fqn: str, columns: list, sample_data: list) -> str:
    """Build a deterministic INSERT OVERWRITE statement from catalog rows."""
    if not sample_data:
        return ""
    if not columns:
        return ""
    col_clause = "(" + ", ".join(columns) + ")"
    row_strs = []
    for row in sample_data:
        vals = [_sql_literal(row.get(c)) for c in columns]
        row_strs.append("(" + ", ".join(vals) + ")")
    return (
        f"-- Catalog-enforced seed for {target_fqn} ({len(sample_data)} rows from utility_catalog.sample_data)\n"
        f"INSERT OVERWRITE {target_fqn} {col_clause} VALUES\n  "
        + ",\n  ".join(row_strs)
        + ";"
    )


def _strip_table_quotes(s: str) -> str:
    return s.strip().strip("`\"")


def _enforce_catalog_seed_inserts(sql: str, catalog: dict) -> str:
    """For every `INSERT INTO/OVERWRITE <fqn> [(cols)] VALUES ...;` in the SQL:
      - If <fqn> is in the catalog AND has sample_data → replace VALUES with
        a deterministic INSERT OVERWRITE that uses catalog.sample_data verbatim.
      - If <fqn> is in the catalog AND has NO sample_data → drop the statement
        and leave a comment explaining why (we do NOT fabricate rows).
      - If <fqn> is NOT in the catalog → leave the statement alone (it's not a
        catalog-tracked seed; could be a derived intermediate).

    Catalog FQNs may be the design-time form (`bp_*.X.Y`) or the env-rewritten
    form; we tolerate both by comparing both shapes.
    """
    if not sql or not isinstance(catalog, dict):
        return sql

    seed_index = _build_catalog_seed_index(catalog)
    if not seed_index:
        return sql

    # Also tolerate the env-catalog-rewritten form: build a parallel index
    # keyed by the rewritten name so a lookup on either form succeeds.
    env_catalog = (os.environ.get("DATABRICKS_CATALOG") or "").strip()
    extended_index = dict(seed_index)
    if env_catalog and env_catalog not in _REWRITABLE_CATALOGS:
        for fqn, entry in list(seed_index.items()):
            rewritten = fqn
            for old in _REWRITABLE_CATALOGS:
                rewritten = rewritten.replace(f"{old}.", f"{env_catalog}.")
            if rewritten != fqn:
                extended_index[rewritten] = entry

    # Match INSERT INTO/OVERWRITE <table> [(cols)] VALUES <rows> ;
    pattern = re.compile(
        r"INSERT\s+(?:OVERWRITE\s+)?(?:INTO\s+)?"
        r"([`\"\w.]+)"               # target table (group 1)
        r"\s*(\([^)]*\))?"           # optional column list (group 2)
        r"\s*VALUES\s*"
        r"(.+?);",                   # values payload through to the semicolon (group 3)
        re.IGNORECASE | re.DOTALL,
    )

    def _replace(m: "re.Match") -> str:
        raw_target = m.group(1)
        target = _strip_table_quotes(raw_target)
        col_list_part = m.group(2) or ""
        entry = extended_index.get(target)
        if entry is None:
            # Not in catalog — leave alone (could be a non-seed insert)
            return m.group(0)
        sample = entry.get("sample_data") or []
        if not sample:
            return (
                f"-- removed: INSERT VALUES for {target} "
                f"(utility_catalog has the table but no sample_data; "
                f"no rows will be seeded)\n"
            )
        # Prefer the explicit column list the LLM wrote, but if any column
        # name isn't in the catalog, fall back to the catalog's full column list.
        catalog_cols = entry.get("columns") or []
        if col_list_part:
            inner = col_list_part.strip("()").strip()
            cols = [_strip_table_quotes(c) for c in inner.split(",") if c.strip()]
            if not all(c in catalog_cols for c in cols):
                cols = list(catalog_cols)
        else:
            cols = list(catalog_cols)
        if not cols:
            return (
                f"-- removed: INSERT VALUES for {target} "
                f"(no column list available)\n"
            )
        return _build_insert_from_catalog(target, cols, sample)

    return pattern.sub(_replace, sql)
