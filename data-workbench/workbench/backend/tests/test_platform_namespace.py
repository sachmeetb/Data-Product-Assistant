"""Platform namespace model: depth/quoting/parse/deploy semantics per platform,
and parity with the stdlib runner mirror that consumes its serialised descriptor.

These lock in the "platform-specific detail lives in the platform layer" contract
so a new platform is added by shipping a manifest, not by editing shared code.
"""
import pytest

from workbench.backend.platform.namespace import get_namespace_model
from workbench.backend.serving_runners._deploy_core import _NamespaceOps


def test_levels_from_manifest():
    # 2-level engines vs 3-level catalog engines — read from manifest parts.
    assert get_namespace_model("postgres").levels == 2
    assert get_namespace_model("mysql").levels == 2
    assert get_namespace_model("databricks").levels == 3
    assert get_namespace_model("snowflake").levels == 3


def test_postgresql_alias_resolves():
    assert get_namespace_model("postgresql").platform_id == "postgres"


def test_fail_closed_unknown_platform():
    with pytest.raises(KeyError):
        get_namespace_model("teradata")


def test_qualify_quotes_each_part_by_platform():
    db = get_namespace_model("databricks")
    assert db.qualify("workspace.default", "vw_x") == "`workspace`.`default`.`vw_x`"
    pg = get_namespace_model("postgres")
    assert pg.qualify("public", "vw_x") == '"public"."vw_x"'


def test_parse_right_aligns_to_container_parts():
    db = get_namespace_model("databricks")   # containers: catalog, schema
    assert db.parse("samples.bakehouse") == {"catalog": "samples", "schema": "bakehouse"}
    assert db.parse("bakehouse") == {"schema": "bakehouse"}   # 1 part → innermost
    pg = get_namespace_model("postgres")      # container: schema
    assert pg.parse("public") == {"schema": "public"}


def test_session_setup_only_for_declared_catalog_platforms():
    # Databricks declares USE CATALOG; the source catalog fills it.
    assert get_namespace_model("databricks").session_setup_statements("samples.bakehouse") == [
        "USE CATALOG `samples`"
    ]
    # Postgres declares nothing → no session setup.
    assert get_namespace_model("postgres").session_setup_statements("public") == []


def test_create_namespace_statement():
    assert (get_namespace_model("databricks").create_namespace_statement("workspace.default")
            == "CREATE SCHEMA IF NOT EXISTS `workspace`.`default`")
    assert (get_namespace_model("postgres").create_namespace_statement("public")
            == 'CREATE SCHEMA IF NOT EXISTS "public"')


@pytest.mark.parametrize("platform", ["postgres", "mysql", "snowflake", "databricks"])
def test_runner_mirror_parity(platform):
    """The stdlib runner (_NamespaceOps) must reproduce the backend model EXACTLY
    from the serialised descriptor — the runner can't import the model, so drift
    here silently breaks deploy on the downloaded package."""
    m = get_namespace_model(platform)
    r = _NamespaceOps(m.to_descriptor())
    assert r.container_parts == m.container_parts
    assert r.qualified("a.b", "v") == m.qualify("a.b", "v")
    assert r.parse("a.b") == m.parse("a.b")
    assert r.session_setup("a.b") == m.session_setup_statements("a.b")
    assert r.create_namespace("a.b") == m.create_namespace_statement("a.b")
