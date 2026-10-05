"""DQ dual-mode on-disk suite separation (Phase 2, item 1).

A `dpe-sa` product can hold BOTH a catalog (source pre-check) suite and a dprod
(deployed-product) suite. These must land in separate directories so their Builds
don't clobber each other and the readers can tell them apart. These tests exercise
the pure disk-layout helpers (`dq_subdir`, `available_dq_suites`, `resolve_suite`,
`collect_dq_files`, `assemble_dq_package`) — no Neo4j/Postgres needed.
"""

from __future__ import annotations

from workbench.backend import config as _cfg
from workbench.backend.dq_test_executor import dq_subdir


def test_dq_subdir_suffixes_only_dprod():
    assert dq_subdir("dq_tests_gx", "catalog") == "dq_tests_gx"
    assert dq_subdir("dq_tests_python", "catalog") == "dq_tests_python"
    assert dq_subdir("dq_tests_gx", "dprod") == "dq_tests_gx_dprod"
    assert dq_subdir("dq_tests_python", "dprod") == "dq_tests_python_dprod"


def _make_suite(base_dir, subdir: str, entry_script: str):
    d = base_dir / subdir
    d.mkdir(parents=True, exist_ok=True)
    (d / entry_script).write_text("# generated test entrypoint\n")
    (d / "suite_public_orders.py").write_text("# a validator\n")
    # a per-run results dir that must be excluded from the package
    (d / "results").mkdir(exist_ok=True)
    (d / "results" / "run_result.json").write_text("{}")


def test_suites_coexist_without_collision(tmp_path, monkeypatch):
    """Catalog GX + dprod GX suites live in distinct dirs and are each discoverable."""
    from workbench.backend import dq_package

    code = "sa-01012026-01"
    monkeypatch.setattr(_cfg, "BASE_PROJECT_DIR", tmp_path)
    monkeypatch.setattr(dq_package, "BASE_PROJECT_DIR", tmp_path)

    proj = tmp_path / code
    _make_suite(proj, "dq_tests_gx", "run_gx_validations.py")          # catalog
    _make_suite(proj, "dq_tests_gx_dprod", "run_gx_validations.py")    # dprod

    suites = dq_package.available_dq_suites(code)
    modes = {s["source_mode"] for s in suites}
    assert modes == {"catalog", "dprod"}
    # dprod is listed first so single-default callers treat the product suite as primary
    assert suites[0]["source_mode"] == "dprod"

    # Explicit mode picks the matching suite.
    assert dq_package.detect_active_framework(code, "catalog") == "gx"
    assert dq_package.detect_active_framework(code, "dprod") == "gx"
    assert dq_package.resolve_suite(code, "catalog") == ("gx", "catalog")
    assert dq_package.resolve_suite(code, "dprod") == ("gx", "dprod")
    # No explicit mode → dprod preferred.
    assert dq_package.resolve_suite(code, None) == ("gx", "dprod")


def test_collect_and_assemble_scope_to_mode(tmp_path, monkeypatch):
    from workbench.backend import dq_package

    code = "sa-01012026-02"
    monkeypatch.setattr(_cfg, "BASE_PROJECT_DIR", tmp_path)
    monkeypatch.setattr(dq_package, "BASE_PROJECT_DIR", tmp_path)

    proj = tmp_path / code
    _make_suite(proj, "dq_tests_gx", "run_gx_validations.py")
    _make_suite(proj, "dq_tests_gx_dprod", "run_gx_validations.py")

    catalog_files = dq_package.collect_dq_files(code, "gx", "catalog")
    dprod_files = dq_package.collect_dq_files(code, "gx", "dprod")
    # Both suites collect the entry script + validator, and exclude results/.
    assert "run_gx_validations.py" in catalog_files
    assert "run_gx_validations.py" in dprod_files
    assert not any(k.startswith("results/") for k in catalog_files)
    assert not any(k.startswith("results/") for k in dprod_files)

    dprod_pkg = dq_package.assemble_dq_package(code, "gx", "dprod")
    assert dprod_pkg is not None
    # dprod README carries the deployed-product-views note.
    assert "deployed product views" in dprod_pkg["README.md"]

    catalog_pkg = dq_package.assemble_dq_package(code, "gx", "catalog")
    assert "deployed product views" not in catalog_pkg["README.md"]


def test_dprod_only_product_resolves_without_explicit_mode(tmp_path, monkeypatch):
    """A dpe-cf product has only the dprod suite; catalog detect must not shadow it."""
    from workbench.backend import dq_package

    code = "cf-01012026-01"
    monkeypatch.setattr(_cfg, "BASE_PROJECT_DIR", tmp_path)
    monkeypatch.setattr(dq_package, "BASE_PROJECT_DIR", tmp_path)

    proj = tmp_path / code
    _make_suite(proj, "dq_tests_python_dprod", "run_all.py")

    assert dq_package.detect_active_framework(code, "catalog") is None
    assert dq_package.detect_active_framework(code, "dprod") == "pandera"
    # No explicit mode + a catalog default request both fall back to the present suite.
    assert dq_package.resolve_suite(code, None) == ("pandera", "dprod")
    assert dq_package.resolve_suite(code, "catalog") == ("pandera", "dprod")


# ── Tier 6: catalog-mode DQ connection is platform-aware ────────────────────────

def test_catalog_conn_string_uses_registered_snowflake_source():
    """A registered non-Postgres SourceBinding (Snowflake) makes catalog DQ resolve
    a snowflake:// DSN + preflight the dialect, instead of the legacy pg_connection."""
    import json as _json
    from sqlmodel import Session
    from workbench.backend.database import engine
    from workbench.backend.models import Project, PlatformConnection, SourceBinding
    from workbench.backend.dq_test_executor import _resolve_catalog_conn_string
    with Session(engine) as s:
        proj = Project(project_code="dq-sf-catalog", name="p",
                       pg_connection="postgresql://legacy@h/d")
        s.add(proj); s.commit(); s.refresh(proj)
        conn = PlatformConnection(
            connection_name="dq-sf-src", platform_type="snowflake",
            host="org-acct", port=443, database="DWB_SERVING_DB", username="NEYDE",
            secret_ref="direct:pat",
            extra_config_json=_json.dumps({"warehouse": "COMPUTE_WH", "role": "R"}),
        )
        s.add(conn); s.commit(); s.refresh(conn)
        s.add(SourceBinding(project_id=proj.id, connection_id=conn.id, default_schema="PUBLIC"))
        s.commit()
        proj = s.get(Project, proj.id)
    dsn, err = _resolve_catalog_conn_string(proj)
    # snowflake-sqlalchemy is installed in the test env, so the preflight passes.
    assert err is None
    assert dsn.startswith("snowflake://NEYDE:")
    assert "org-acct/DWB_SERVING_DB" in dsn
    assert "warehouse=COMPUTE_WH" in dsn
    with Session(engine) as s:
        for row in s.exec(__import__("sqlmodel").select(SourceBinding)).all():
            s.delete(row)
        for row in s.exec(__import__("sqlmodel").select(PlatformConnection)).all():
            s.delete(row)
        s.commit()


def test_catalog_conn_string_resolves_from_source_binding():
    """Catalog DQ resolves the SOURCE DSN from the structured SourceBinding →
    PlatformConnection (the single connection contract) — not a legacy DSN."""
    from sqlmodel import Session, select
    from workbench.backend.database import engine
    from workbench.backend.models import Project, PlatformConnection, SourceBinding
    from workbench.backend.dq_test_executor import _resolve_catalog_conn_string

    with Session(engine) as session:
        conn = PlatformConnection(
            connection_name="dq-catalog-src", platform_type="postgres",
            host="h", port=5432, database="d", username="u",
            secret_ref="direct:p",
        )
        session.add(conn)
        session.commit()
        session.refresh(conn)
        proj = Project(project_code="dq-struct-catalog", name="p", pg_connection="")
        session.add(proj)
        session.commit()
        session.refresh(proj)
        session.add(SourceBinding(project_id=proj.id, connection_id=conn.id,
                                  default_schema="public"))
        session.commit()
        proj_id = proj.id

    with Session(engine) as session:
        proj = session.get(Project, proj_id)
        dsn, err = _resolve_catalog_conn_string(proj)
    assert err is None
    assert dsn.startswith("postgresql://u:p@h:5432/d")
    # The retired legacy shape must not surface anywhere in the resolved DSN key.
    assert "pg_connection" not in (dsn or "")
