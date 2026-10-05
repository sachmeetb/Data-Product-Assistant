"""Tests for multi-platform dbt profiles.yml generation (generate_dbt_project.py).

Coverage:
- _get_profiles_yml: correct adapter type and key fields for all four platforms
- _get_profiles_yml: fail-closed on unknown dialect (ADR-9)
- _get_profiles_yml: "postgresql" alias resolves to postgres template
- scaffold(): produces the right adapter type for each dialect
- scaffold(): writes both prod and preview targets
- scaffold(): backward compat — default dialect_name="postgres"
- _PROFILES_YML alias still equals _PROFILES_YML_POSTGRES (back-compat)
"""
import importlib.util
import os
import pathlib
import sys
import tempfile

import pytest
import yaml

_SCRIPT = pathlib.Path(__file__).resolve().parents[3] / (
    "workbench-skills/skills/data-serving-virtual-view/scripts/generate_dbt_project.py"
)
# generate_dbt_project.py does `import generate_view_ddl` (bare name) — add the
# scripts directory to sys.path so the sibling script resolves.
_SCRIPTS_DIR = str(_SCRIPT.parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

_spec = importlib.util.spec_from_file_location("generate_dbt_project", _SCRIPT)
dg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dg)


# ── _get_profiles_yml ─────────────────────────────────────────────────────────

class TestGetProfilesYml:
    def test_postgres_adapter_type(self):
        yml = dg._get_profiles_yml("postgres", "analytics")
        assert "type: postgres" in yml

    def test_postgres_host_env_var(self):
        yml = dg._get_profiles_yml("postgres", "analytics")
        assert "WB_DBT_HOST" in yml

    def test_postgres_schema_substituted(self):
        yml = dg._get_profiles_yml("postgres", "my_schema")
        assert "schema: my_schema" in yml
        assert "schema: my_schema_preview" in yml

    def test_postgres_no_literal_braces_left(self):
        yml = dg._get_profiles_yml("postgres", "s")
        assert "{schema}" not in yml

    def test_postgresql_alias_same_as_postgres(self):
        pg = dg._get_profiles_yml("postgres", "s")
        pql = dg._get_profiles_yml("postgresql", "s")
        assert pg == pql

    def test_snowflake_adapter_type(self):
        yml = dg._get_profiles_yml("snowflake", "analytics")
        assert "type: snowflake" in yml

    def test_snowflake_uses_account_not_host(self):
        yml = dg._get_profiles_yml("snowflake", "analytics")
        assert "WB_DBT_ACCOUNT" in yml
        assert "WB_DBT_HOST" not in yml

    def test_snowflake_warehouse_field(self):
        yml = dg._get_profiles_yml("snowflake", "analytics")
        assert "WB_DBT_WAREHOUSE" in yml

    def test_snowflake_no_port_field(self):
        yml = dg._get_profiles_yml("snowflake", "analytics")
        assert "port" not in yml

    def test_snowflake_schema_substituted(self):
        yml = dg._get_profiles_yml("snowflake", "sf_schema")
        assert "schema: sf_schema" in yml
        assert "schema: sf_schema_preview" in yml

    def test_databricks_adapter_type(self):
        yml = dg._get_profiles_yml("databricks", "analytics")
        assert "type: databricks" in yml

    def test_databricks_http_path_field(self):
        yml = dg._get_profiles_yml("databricks", "analytics")
        assert "WB_DBT_HTTP_PATH" in yml

    def test_databricks_token_field(self):
        yml = dg._get_profiles_yml("databricks", "analytics")
        assert "WB_DBT_TOKEN" in yml

    def test_databricks_no_password_field(self):
        yml = dg._get_profiles_yml("databricks", "analytics")
        assert "WB_DBT_PASSWORD" not in yml

    def test_databricks_catalog_field(self):
        yml = dg._get_profiles_yml("databricks", "analytics")
        assert "WB_DBT_CATALOG" in yml

    def test_databricks_schema_substituted(self):
        yml = dg._get_profiles_yml("databricks", "db_schema")
        assert "schema: db_schema" in yml
        assert "schema: db_schema_preview" in yml

    def test_mysql_adapter_type(self):
        yml = dg._get_profiles_yml("mysql", "analytics")
        assert "type: mysql" in yml

    def test_mysql_uses_server_not_host_field(self):
        yml = dg._get_profiles_yml("mysql", "analytics")
        assert "server:" in yml

    def test_mysql_uses_username_not_user(self):
        yml = dg._get_profiles_yml("mysql", "analytics")
        assert "username:" in yml
        assert "user:" not in yml

    def test_mysql_uses_database_field(self):
        yml = dg._get_profiles_yml("mysql", "analytics")
        assert "database:" in yml

    def test_mysql_default_port_3306(self):
        yml = dg._get_profiles_yml("mysql", "analytics")
        assert "3306" in yml

    def test_mysql_schema_substituted(self):
        yml = dg._get_profiles_yml("mysql", "my_schema")
        assert "schema: my_schema" in yml
        assert "schema: my_schema_preview" in yml

    def test_unknown_dialect_raises_value_error(self):
        with pytest.raises(ValueError, match="No dbt profiles.yml template"):
            dg._get_profiles_yml("redshift", "s")

    def test_fail_closed_on_bigquery(self):
        with pytest.raises(ValueError):
            dg._get_profiles_yml("bigquery", "s")

    def test_all_templates_have_prod_and_preview(self):
        for dialect in ("postgres", "snowflake", "databricks", "mysql"):
            yml = dg._get_profiles_yml(dialect, "s")
            assert "prod:" in yml, f"{dialect}: missing prod target"
            assert "preview:" in yml, f"{dialect}: missing preview target"

    def test_all_templates_have_wb_profile_header(self):
        for dialect in ("postgres", "snowflake", "databricks", "mysql"):
            yml = dg._get_profiles_yml(dialect, "s")
            assert yml.startswith("wb_profile:"), f"{dialect}: wrong header"

    def test_all_templates_have_threads(self):
        for dialect in ("postgres", "snowflake", "databricks", "mysql"):
            yml = dg._get_profiles_yml(dialect, "s")
            assert "threads:" in yml, f"{dialect}: missing threads"


# ── YAML-structural load (dbt-core 1.11 parses profiles.yml as YAML) ────────────

class TestProfilesYamlStructural:
    """dbt-core 1.11 loads profiles.yml as YAML *before* rendering Jinja, so a
    `{% %}` control-flow block at a mapping-key position blows up the scanner.
    These tests reproduce that structural load and would have caught the
    Snowflake SSO-authenticator regression (ScannerError near the `%`)."""

    def test_all_templates_parse_as_yaml(self):
        for dialect in ("postgres", "snowflake", "databricks", "mysql"):
            raw = dg._get_profiles_yml(dialect, "s")
            # yaml.safe_load must succeed — a stray `{% %}` block would raise
            # ScannerError here (the reported bug), value-interpolated `{{ }}`
            # scalars inside double quotes parse fine.
            doc = yaml.safe_load(raw)
            outputs = doc["wb_profile"]["outputs"]
            assert "prod" in outputs, f"{dialect}: missing prod output"
            assert "preview" in outputs, f"{dialect}: missing preview output"

    def test_snowflake_has_no_jinja_control_flow(self):
        raw = dg._get_profiles_yml("snowflake", "s")
        assert "{%" not in raw, "snowflake template must not use `{% %}` control-flow"

    def test_snowflake_always_emits_authenticator(self):
        raw = dg._get_profiles_yml("snowflake", "s")
        # One `authenticator:` line per target (prod + preview).
        assert raw.count("authenticator:") == 2
        doc = yaml.safe_load(raw)
        outputs = doc["wb_profile"]["outputs"]
        assert "authenticator" in outputs["prod"]
        assert "authenticator" in outputs["preview"]


# ── backward compat ────────────────────────────────────────────────────────────

class TestBackwardCompat:
    def test_profiles_yml_alias_equals_postgres(self):
        assert dg._PROFILES_YML == dg._PROFILES_YML_POSTGRES

    def test_profiles_yml_contains_type_postgres(self):
        assert "type: postgres" in dg._PROFILES_YML


# ── scaffold() ────────────────────────────────────────────────────────────────

class TestScaffoldDialect:
    """scaffold() writes the correct profiles.yml for each dialect."""

    def _run_scaffold(self, dialect_name):
        with tempfile.TemporaryDirectory() as tmp:
            dg.scaffold(
                output_dir=tmp,
                project_code="test_proj",
                product_uri="dprod:test",
                models=[],
                schema="test_schema",
                materialization="table",
                dialect_name=dialect_name,
            )
            with open(os.path.join(tmp, "profiles.yml")) as f:
                return f.read()

    def test_default_is_postgres(self):
        with tempfile.TemporaryDirectory() as tmp:
            dg.scaffold(
                output_dir=tmp,
                project_code="test_proj",
                product_uri="dprod:test",
                models=[],
                schema="test_schema",
                materialization="table",
                # dialect_name omitted — should default to postgres
            )
            with open(os.path.join(tmp, "profiles.yml")) as f:
                yml = f.read()
        assert "type: postgres" in yml

    def test_snowflake_profile_written(self):
        yml = self._run_scaffold("snowflake")
        assert "type: snowflake" in yml

    def test_databricks_profile_written(self):
        yml = self._run_scaffold("databricks")
        assert "type: databricks" in yml

    def test_mysql_profile_written(self):
        yml = self._run_scaffold("mysql")
        assert "type: mysql" in yml

    def test_scaffold_unknown_dialect_raises(self):
        with pytest.raises(ValueError, match="No dbt profiles.yml template"):
            self._run_scaffold("oracle")

    def test_schema_in_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            dg.scaffold(
                output_dir=tmp,
                project_code="p",
                product_uri="u",
                models=[],
                schema="prod_schema",
                materialization="table",
                dialect_name="postgres",
            )
            with open(os.path.join(tmp, "profiles.yml")) as f:
                yml = f.read()
        assert "schema: prod_schema" in yml
        assert "schema: prod_schema_preview" in yml
