"""Tests for serving_docs (README generation) + serving_package fallbacks.

The skill call itself needs the SDK; these tests cover the deterministic bits:
the ``{files}`` parser and the Python fallback READMEs that guarantee a complete
package when the skill is unavailable.
"""
from __future__ import annotations

from pathlib import Path

from workbench.backend import serving_docs, serving_package


# ── skill output parser ─────────────────────────────────────────────────────


def test_parse_files_extracts_readme():
    text = 'prose\n```json\n{"files": {"README.md": "# Hi"}}\n```\n'
    assert serving_docs._parse_files(text) == {"README.md": "# Hi"}


def test_parse_files_tolerates_bare_readme():
    text = '```json\n{"README.md": "# Bare"}\n```'
    assert serving_docs._parse_files(text) == {"README.md": "# Bare"}


def test_parse_files_empty_on_garbage():
    assert serving_docs._parse_files("no json here") == {}


def test_sync_wrapper_returns_none_on_skill_absence(monkeypatch):
    # With no SDK/skill, the generator returns None → caller uses the fallback.
    async def _none(**kw):
        return None
    monkeypatch.setattr(serving_docs, "generate_dbt_readme", _none)
    assert serving_docs.generate_dbt_readme_sync(
        product_name="P", description="d", platform="postgres", model_names=["m"]) is None


# ── deterministic fallback READMEs ─────────────────────────────────────────────


def test_view_fallback_readme_names_views():
    md = serving_package._fallback_view_readme(
        "hr-proj", "mysql", "public", ["vw_employees"], product_name="HR")
    assert "HR" in md and "vw_employees" in md
    assert "python run.py --apply" in md


def test_dbt_fallback_readme_names_models():
    md = serving_package._fallback_dbt_readme("p", "postgres", ["vw_a", "vw_b"])
    assert "vw_a" in md and "bootstrap.py" in md


def test_lakehouse_fallback_readme_names_datasets():
    md = serving_package._fallback_lakehouse_readme(
        "p", [{"model_name": "vw_x"}], product_name="Prod")
    assert "vw_x" in md and "query.py" in md


# ── assemble writes a complete package ──────────────────────────────────────────


def test_assemble_view_package_writes_runner_and_meta(tmp_path, monkeypatch):
    monkeypatch.setattr(serving_package, "BASE_PROJECT_DIR", tmp_path)
    pkg = serving_package.assemble_view_package(
        "proj", ddl='CREATE VIEW "public"."vw_x" AS SELECT 1', view_schema="public",
        view_names=["vw_x"], platform="mysql")
    names = {p.name for p in Path(pkg).iterdir()}
    assert {"run.py", "_wb_runresult.py", "_deploy_core.py", "view.sql",
            "package.json", "requirements.txt", ".env.example", "README.md"} <= names
    assert "pymysql" in (Path(pkg) / "requirements.txt").read_text()


def test_assemble_lakehouse_package_writes_query_and_models(tmp_path):
    models = [{"physical_name": "employees", "model_name": "vw_employees",
               "select_body": "SELECT 1"}]
    pkg = serving_package.assemble_lakehouse_package(
        tmp_path / "lh", project_code="proj", models=models)
    names = {p.name for p in Path(pkg).iterdir()}
    assert {"run.py", "_wb_runresult.py", "query.py", "explore.sql", "models.json",
            "requirements.txt", ".env.example", "README.md"} <= names
