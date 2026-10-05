"""Scaffold a dbt project that materializes a data product as physical tables.

This is the dbt EMITTER half of the "one SQL core, two emitters" design. It
reuses `generate_view_ddl.generate_dbt_models()` — the identical per-dataset
compiler the virtual-view path uses — so the transform DSL (:ColumnMapping +
:DatasetTransform CTE layers, FK-inferred joins, dialect casts, SCD lowering)
is compiled in exactly ONE place. The only difference from view serving is the
wrapper: a dbt model file ({{ config(...) }} + bare SELECT body) instead of a
`CREATE OR REPLACE VIEW … AS …;`.

Output layout (written under --output-dir, default <project>/dbt):

    dbt_project.yml
    profiles.yml                 # prod + preview targets; platform-specific
    macros/generate_schema_name.sql
    models/<model>.sql           # one per :DProdOutputDataset
    models/schema.yml            # model docs (description per model)

The backend (routers/materialization.py) invokes this as a subprocess, then
runs `dbt build` against the generated project. The project folder is fully
self-contained so it can later be zipped and handed to the engineer to run in
their own Claude Code / Codex session (Phase: downloadable export).

Materialization strategy for v1 is `table` (full refresh). `--sample-limit`
emits a LIMIT guarded by a dbt var so a capped "preview" build is possible
(wired now, exercised by the Phase-2 verification gate).

profiles.yml is dialect-aware (Phase 4): the ``--dialect`` flag selects the
dbt adapter type.  Connection secrets are read from platform-specific
``WB_DBT_*`` env vars at ``dbt build`` time — see ``_PROFILES_YML_*`` for the
per-platform env var contract.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from neo4j import GraphDatabase

import generate_view_ddl as gv


_PROJECT_YML = """\
name: '{project_name}'
version: '1.0.0'
config-version: 2
profile: 'wb_profile'
model-paths: ["models"]
macro-paths: ["macros"]
snapshot-paths: ["snapshots"]
target-path: "target"
log-path: "logs"
clean-targets: ["target", "dbt_packages"]
quoting:
  database: false
  schema: false
  identifier: false
models:
  {project_name}:
    +materialized: {materialization}
"""

# Override dbt's default schema-name generation so a model's custom schema (or
# the target schema when none is set) is used VERBATIM — dbt's stock macro
# concatenates target.schema + custom, which would scatter our tables into
# `public_<schema>`. We want the table in exactly the schema the engineer chose.
_SCHEMA_MACRO = """\
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if custom_schema_name is none -%}
        {{ target.schema }}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
"""

# ── per-platform profiles.yml templates ───────────────────────────────────────
# Connection secrets are read from env vars at `dbt build` time, NOT baked into
# the file — so the generated project folder can be zipped and handed to the
# engineer without leaking credentials.  The backend injects WB_DBT_* into the
# dbt subprocess env; an engineer running locally sets the same vars.
#
# Each template uses {schema} as a Python .format() placeholder and
# {{{{ ... }}}} to produce dbt/Jinja2 {{ ... }} expressions in the output.
#
# Env var contracts by platform:
#   postgres  : WB_DBT_HOST, WB_DBT_PORT, WB_DBT_USER, WB_DBT_PASSWORD,
#               WB_DBT_DBNAME
#   snowflake : WB_DBT_ACCOUNT, WB_DBT_USER, WB_DBT_PASSWORD,
#               WB_DBT_DATABASE, WB_DBT_WAREHOUSE, WB_DBT_ROLE (optional)
#   databricks: WB_DBT_HOST, WB_DBT_HTTP_PATH, WB_DBT_TOKEN,
#               WB_DBT_CATALOG (optional, default hive_metastore)
#   mysql     : WB_DBT_HOST (→ server), WB_DBT_PORT, WB_DBT_USER (→ username),
#               WB_DBT_PASSWORD, WB_DBT_DBNAME (→ database)

_PROFILES_YML_POSTGRES = """\
wb_profile:
  target: prod
  outputs:
    prod:
      type: postgres
      host: "{{{{ env_var('WB_DBT_HOST', 'localhost') }}}}"
      port: "{{{{ env_var('WB_DBT_PORT', '5432') | as_number }}}}"
      user: "{{{{ env_var('WB_DBT_USER') }}}}"
      password: "{{{{ env_var('WB_DBT_PASSWORD') }}}}"
      dbname: "{{{{ env_var('WB_DBT_DBNAME') }}}}"
      schema: {schema}
      threads: 4
      connect_timeout: 30
    preview:
      type: postgres
      host: "{{{{ env_var('WB_DBT_HOST', 'localhost') }}}}"
      port: "{{{{ env_var('WB_DBT_PORT', '5432') | as_number }}}}"
      user: "{{{{ env_var('WB_DBT_USER') }}}}"
      password: "{{{{ env_var('WB_DBT_PASSWORD') }}}}"
      dbname: "{{{{ env_var('WB_DBT_DBNAME') }}}}"
      schema: {schema}_preview
      threads: 4
      connect_timeout: 30
"""

# Back-compat alias used by the existing scaffold() code before dialect support.
_PROFILES_YML = _PROFILES_YML_POSTGRES

# Auth defaults to PAT/password (a Snowflake PAT authenticates as a plain
# password). WB_DBT_AUTHENTICATOR is OPTIONAL and only for SSO/OAuth
# (externalbrowser / oauth / snowflake_jwt). The `authenticator:` line is ALWAYS
# emitted as a quoted, value-interpolated scalar: unset renders `authenticator: ""`,
# and dbt-snowflake only forwards it `if self.authenticator:` — so an empty string
# is treated identically to unset (the password/PAT default path is unaffected).
# A `{% if %}` control-flow guard was REMOVED because dbt-core 1.11 parses
# profiles.yml as YAML first and rejects a `{% %}` block sitting where a mapping
# key is expected (ScannerError on the `%`) — a value-interpolated scalar is valid
# YAML, a control-flow block is not.
_PROFILES_YML_SNOWFLAKE = """\
wb_profile:
  target: prod
  outputs:
    prod:
      type: snowflake
      account: "{{{{ env_var('WB_DBT_ACCOUNT') }}}}"
      user: "{{{{ env_var('WB_DBT_USER') }}}}"
      password: "{{{{ env_var('WB_DBT_PASSWORD') }}}}"
      database: "{{{{ env_var('WB_DBT_DATABASE') }}}}"
      warehouse: "{{{{ env_var('WB_DBT_WAREHOUSE') }}}}"
      role: "{{{{ env_var('WB_DBT_ROLE', '') }}}}"
      authenticator: "{{{{ env_var('WB_DBT_AUTHENTICATOR', '') }}}}"
      schema: {schema}
      threads: 4
    preview:
      type: snowflake
      account: "{{{{ env_var('WB_DBT_ACCOUNT') }}}}"
      user: "{{{{ env_var('WB_DBT_USER') }}}}"
      password: "{{{{ env_var('WB_DBT_PASSWORD') }}}}"
      database: "{{{{ env_var('WB_DBT_DATABASE') }}}}"
      warehouse: "{{{{ env_var('WB_DBT_WAREHOUSE') }}}}"
      role: "{{{{ env_var('WB_DBT_ROLE', '') }}}}"
      authenticator: "{{{{ env_var('WB_DBT_AUTHENTICATOR', '') }}}}"
      schema: {schema}_preview
      threads: 4
"""

# dbt-databricks uses token auth + Databricks SQL endpoint http_path.
_PROFILES_YML_DATABRICKS = """\
wb_profile:
  target: prod
  outputs:
    prod:
      type: databricks
      host: "{{{{ env_var('WB_DBT_HOST') }}}}"
      http_path: "{{{{ env_var('WB_DBT_HTTP_PATH') }}}}"
      token: "{{{{ env_var('WB_DBT_TOKEN') }}}}"
      catalog: "{{{{ env_var('WB_DBT_CATALOG', 'hive_metastore') }}}}"
      schema: {schema}
      threads: 4
    preview:
      type: databricks
      host: "{{{{ env_var('WB_DBT_HOST') }}}}"
      http_path: "{{{{ env_var('WB_DBT_HTTP_PATH') }}}}"
      token: "{{{{ env_var('WB_DBT_TOKEN') }}}}"
      catalog: "{{{{ env_var('WB_DBT_CATALOG', 'hive_metastore') }}}}"
      schema: {schema}_preview
      threads: 4
"""

# dbt-mysql uses `server` (not host), `username` (not user), `database` (not
# dbname) — field names differ from the Postgres convention.
_PROFILES_YML_MYSQL = """\
wb_profile:
  target: prod
  outputs:
    prod:
      type: mysql
      server: "{{{{ env_var('WB_DBT_HOST', 'localhost') }}}}"
      port: "{{{{ env_var('WB_DBT_PORT', '3306') | as_number }}}}"
      username: "{{{{ env_var('WB_DBT_USER') }}}}"
      password: "{{{{ env_var('WB_DBT_PASSWORD') }}}}"
      database: "{{{{ env_var('WB_DBT_DBNAME') }}}}"
      schema: {schema}
      threads: 4
    preview:
      type: mysql
      server: "{{{{ env_var('WB_DBT_HOST', 'localhost') }}}}"
      port: "{{{{ env_var('WB_DBT_PORT', '3306') | as_number }}}}"
      username: "{{{{ env_var('WB_DBT_USER') }}}}"
      password: "{{{{ env_var('WB_DBT_PASSWORD') }}}}"
      database: "{{{{ env_var('WB_DBT_DBNAME') }}}}"
      schema: {schema}_preview
      threads: 4
"""

_PROFILES_TEMPLATES: dict[str, str] = {
    "postgres":   _PROFILES_YML_POSTGRES,
    "postgresql": _PROFILES_YML_POSTGRES,  # alias
    "snowflake":  _PROFILES_YML_SNOWFLAKE,
    "databricks": _PROFILES_YML_DATABRICKS,
    "mysql":      _PROFILES_YML_MYSQL,
}


def _get_profiles_yml(dialect_name: str, schema: str) -> str:
    """Return the formatted profiles.yml content for the given dialect.

    Fail-closed (ADR-9): raises ``ValueError`` for unknown dialect names.
    Callers should pass the same dialect name used for SQL generation.
    """
    tmpl = _PROFILES_TEMPLATES.get(dialect_name)
    if tmpl is None:
        known = ", ".join(sorted(_PROFILES_TEMPLATES))
        raise ValueError(
            f"No dbt profiles.yml template for dialect {dialect_name!r}. "
            f"Known dialects: {known}"
        )
    return tmpl.format(schema=schema)

# A model body. The {{ config }} sets the materialization; the SELECT body is
# emitted verbatim from the shared compiler. The trailing LIMIT is guarded by a
# dbt var so a normal `dbt build` is a full refresh and
# `dbt build --vars '{sample_limit: N}'` caps rows for verification.
_MODEL_TEMPLATE = """\
{{{{ config(materialized='{materialization}') }}}}
-- Data product: {product_uri}
-- Output dataset: {physical_name}
-- Generated by generate_dbt_project.py (reuses the data-serving-virtual-view compiler)
{select_body}
{{% if var('sample_limit', none) is not none %}}
LIMIT {{{{ var('sample_limit') }}}}
{{% endif %}}
"""

# SCD2 = a dbt SNAPSHOT. The product's compiled SELECT is the current-state
# query; dbt detects changes across runs and writes dbt_valid_from /
# dbt_valid_to / dbt_scd_id (history a view can't accumulate). `target_schema`
# is omitted so snapshots resolve through the same generate_schema_name macro
# as models — landing in the target's schema (prod vs _preview) automatically.
# Strategy: `timestamp` when an effective/updated column is declared, else
# `check` over all columns.
# NOTE: no `invalidate_hard_deletes` — a capped sample build (LIMIT below)
# returns an arbitrary subset, and hard-delete tracking would mis-close every
# row outside the cap. Leave it default-off so sample/full builds agree.
_SNAPSHOT_TEMPLATE = """\
{{% snapshot {model_name} %}}
{{{{
  config(
    unique_key={unique_key},
    strategy='{strategy}',
    {strategy_arg}
  )
}}}}
-- SCD2 history for data product {product_uri}, output dataset {physical_name}.
-- Re-run `dbt build`/`dbt snapshot` to accrue change history.
{select_body}
{{% if var('sample_limit', none) is not none %}}
LIMIT {{{{ var('sample_limit') }}}}
{{% endif %}}
{{% endsnapshot %}}
"""


def _emit_snapshot(output_dir, model, product_uri):
    """Write a dbt snapshot for an scd2 output dataset. Returns (ok, detail).

    Falls back to a plain table (ok=False) when no PK is available — a snapshot
    needs a unique_key, and silently snapshotting on no key would corrupt
    history.
    """
    summary = model.get("summary") or {}
    pks = summary.get("primary_key_columns") or []
    if not pks:
        return False, "no primary-key column flagged isPrimaryKey — cannot snapshot"

    unique_key = repr(pks[0]) if len(pks) == 1 else "[" + ", ".join(repr(p) for p in pks) + "]"

    eff = (summary.get("scd_effective_column") or "").strip()
    if eff:
        # scd_effective_column is the RAW product-column name, but the compiled
        # SELECT emits it as `... AS {_safe_name(col)}`. The snapshot's
        # updated_at must reference that physical alias, not the raw name —
        # otherwise dbt's timestamp strategy can't find the column.
        strategy = "timestamp"
        strategy_arg = f"updated_at={gv._safe_name(eff)!r},"
    else:
        strategy = "check"
        strategy_arg = "check_cols='all',"

    os.makedirs(os.path.join(output_dir, "snapshots"), exist_ok=True)
    path = os.path.join(output_dir, "snapshots", model["model_name"] + ".sql")
    with open(path, "w") as f:
        f.write(_SNAPSHOT_TEMPLATE.format(
            model_name=model["model_name"],
            unique_key=unique_key,
            strategy=strategy,
            strategy_arg=strategy_arg,
            product_uri=product_uri,
            physical_name=model["physical_name"],
            select_body=model["select_body"],
        ))
    return True, f"strategy={strategy}, unique_key={unique_key}"


def scaffold(*, output_dir, project_code, product_uri, models, schema,
             materialization, dialect_name: str = "postgres"):
    """Write the dbt project files. `models` is generate_dbt_models() output.

    ``dialect_name`` selects the dbt adapter type and the corresponding
    profiles.yml env var contract (see ``_PROFILES_TEMPLATES``).  Defaults to
    ``"postgres"`` for backward compatibility.

    No DB secrets are written — profiles.yml reads WB_DBT_* env vars at build
    time (see ``_get_profiles_yml``).
    """
    project_name = "wb_" + "".join(
        c if (c.isalnum() or c == "_") else "_" for c in project_code
    )

    models_dir = os.path.join(output_dir, "models")
    snapshots_dir = os.path.join(output_dir, "snapshots")
    os.makedirs(models_dir, exist_ok=True)
    os.makedirs(os.path.join(output_dir, "macros"), exist_ok=True)

    # Clear stale .sql from a previous scaffold so a dataset that switched
    # materialization (e.g. table↔snapshot when its scd policy changed) doesn't
    # leave BOTH a model and a snapshot with the same name — dbt errors on the
    # duplicate resource. Idempotent regeneration: the graph is the source of
    # truth for what should exist this run.
    import glob
    stale_files = (
        glob.glob(os.path.join(models_dir, "*.sql"))
        + glob.glob(os.path.join(models_dir, "*.yml"))   # schema.yml: rewritten only when table models exist
        + glob.glob(os.path.join(snapshots_dir, "*.sql"))
    )
    for stale in stale_files:
        try:
            os.remove(stale)
        except OSError:
            pass

    with open(os.path.join(output_dir, "dbt_project.yml"), "w") as f:
        f.write(_PROJECT_YML.format(project_name=project_name,
                                    materialization=materialization))

    with open(os.path.join(output_dir, "profiles.yml"), "w") as f:
        f.write(_get_profiles_yml(dialect_name, schema))

    with open(os.path.join(output_dir, "macros", "generate_schema_name.sql"), "w") as f:
        f.write(_SCHEMA_MACRO)

    model_docs = []
    for m in models:
        summary = m.get("summary") or {}
        is_scd2 = (summary.get("scd_policy_type") == "scd2")
        snap_ok = False
        if is_scd2:
            snap_ok, detail = _emit_snapshot(output_dir, m, product_uri)
            m["kind"] = "snapshot" if snap_ok else "table"
            m["snapshot_detail"] = detail
        else:
            m["kind"] = "table"

        # Emit a plain table model unless an scd2 snapshot took over this
        # dataset (the snapshot relation IS the materialized product table —
        # a sibling table model would collide on the same name).
        if not (is_scd2 and snap_ok):
            model_file = os.path.join(output_dir, "models", m["model_name"] + ".sql")
            with open(model_file, "w") as f:
                f.write(_MODEL_TEMPLATE.format(
                    materialization=materialization,
                    product_uri=product_uri,
                    physical_name=m["physical_name"],
                    select_body=m["select_body"],
                ))
            model_docs.append({
                "name": m["model_name"],
                "description": f"Materialized output dataset {m['physical_name']} "
                               f"for data product {product_uri}.",
            })

    if model_docs:
        with open(os.path.join(output_dir, "models", "schema.yml"), "w") as f:
            f.write("version: 2\nmodels:\n")
            for d in model_docs:
                f.write(f"  - name: {d['name']}\n    description: \"{d['description']}\"\n")

    return project_name


def main():
    p = argparse.ArgumentParser(description="Scaffold a dbt project for product materialization")
    p.add_argument("--product-uri", required=True)
    p.add_argument("--project-code", required=True)
    p.add_argument("--output-dir", required=True, help="dbt project root (e.g. <project>/dbt)")
    p.add_argument("--target-schema", default="public", help="Schema the tables land in")
    p.add_argument("--materialization", default="table",
                   choices=["table", "incremental", "view"])
    p.add_argument("--source-view-schema", default="public",
                   help="Schema where CONSUMES'd source views live (dpe-cf). "
                        "Passed through to the compiler as view_schema. Only a "
                        "FALLBACK — a relation present in --source-served-map wins.")
    p.add_argument("--source-served-map", default="{}",
                   help="JSON map of each CONSUMES'd source dataset physical name → "
                        "its REAL served relation (schema- or catalog.schema-qualified, "
                        "e.g. 'workspace.default.employee'). Mirrors the virtual-view "
                        "path (pg_resolver.resolve_consumed_source_serving). Empty = "
                        "co-located fallback to <source_view_schema>.vw_<name> — a "
                        "Postgres/`public` assumption that breaks on Databricks/Snowflake.")
    p.add_argument("--dialect", default="postgres", choices=sorted(gv._DIALECTS.keys()))
    # Neo4j
    p.add_argument("--host", default="localhost")
    p.add_argument("--bolt-port", type=int, default=7687)
    p.add_argument("--username", default="neo4j")
    p.add_argument("--password", required=True)
    p.add_argument("--database", default="neo4j")
    args = p.parse_args()

    uri = f"bolt://{args.host}:{args.bolt_port}"
    try:
        _served_map_arg = json.loads(args.source_served_map or "{}")
    except (ValueError, TypeError):
        _served_map_arg = {}
    driver = GraphDatabase.driver(uri, auth=(args.username, args.password))
    try:
        try:
            models, summary = gv.generate_dbt_models(
                driver, args.database, args.product_uri,
                view_schema=args.source_view_schema, dialect_name=args.dialect,
                source_served_map=_served_map_arg,
            )
        except gv.ViewGenerationError as e:
            # Known, user-actionable modeling error (e.g. no FK path between
            # mapped tables / declare an explicit join). Emit a clean structured
            # error on stdout — the caller (routers/materialization.py) surfaces
            # `message` as an actionable 422 instead of an opaque 500 with a
            # truncated traceback.
            print(json.dumps({
                "status":      "error",
                "error_class": "ViewGenerationError",
                "message":     str(e),
            }, default=str))
            sys.exit(2)
    finally:
        driver.close()

    if not models:
        print(json.dumps({"status": "failed", "error": summary}))
        sys.exit(1)

    project_name = scaffold(
        output_dir=args.output_dir,
        project_code=args.project_code,
        product_uri=args.product_uri,
        models=models,
        schema=args.target_schema,
        materialization=args.materialization,
        dialect_name=args.dialect,
    )

    print(json.dumps({
        "status": "ok",
        "project_name": project_name,
        "output_dir": args.output_dir,
        "target_schema": args.target_schema,
        "materialization": args.materialization,
        "models": [
            {
                "model_name": m["model_name"],
                "physical_name": m["physical_name"],
                "kind": m.get("kind", "table"),
                "snapshot_detail": m.get("snapshot_detail"),
                # The compiled SELECT body, so the backend can persist a
                # human-readable CREATE TABLE … AS … for the Serving tab.
                "select_body": m.get("select_body"),
            }
            for m in models
        ],
        "summary": summary,
    }, default=str))


if __name__ == "__main__":
    main()
