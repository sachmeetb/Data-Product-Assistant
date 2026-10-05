"""Regression guard: store_serving_definition.extract_view_names must parse the
per-platform-quoted CREATE VIEW header correctly.

The serving DDL header is quoted per dialect — double-quotes for Postgres/Snowflake
(`"db"."schema"."vw"`), backticks for Databricks/MySQL (`` `cat`.`schema`.`vw` ``),
plus Snowflake's trailing `COPY GRANTS`. A naive `.strip('"').rsplit('.')` mangled
a multi-part quoted name into e.g. `"vw_game_stats`, which then got re-quoted at
preview time into invalid SQL (`""vw_game_stats"`). The bare names stored in
:ServingDefinition.viewNames MUST be clean (unquoted), on every platform.
"""
import importlib.util

from workbench.backend.config import BASE_DIR


def _load():
    p = BASE_DIR / "workbench-skills" / "data-serving-virtual-view" / "scripts" / "store_serving_definition.py"
    if not p.exists():
        p = (BASE_DIR / "workbench-skills" / "skills" / "data-serving-virtual-view"
             / "scripts" / "store_serving_definition.py")
    spec = importlib.util.spec_from_file_location("ssd_under_test", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ssd = _load()

_CASES = {
    "snowflake": ('CREATE OR REPLACE VIEW "DWB_SERVING_DB"."PUBLIC"."vw_game_stats" COPY GRANTS AS\nSELECT 1;',
                  [("DWB_SERVING_DB.PUBLIC.vw_game_stats", "vw_game_stats")]),
    "postgres":  ('CREATE OR REPLACE VIEW "public"."vw_x" AS\nSELECT 1;',
                  [("public.vw_x", "vw_x")]),
    "databricks": ('CREATE OR REPLACE VIEW `workspace`.`default`.`vw_x` AS\nSELECT 1;',
                   [("workspace.default.vw_x", "vw_x")]),
    "mysql":     ('CREATE OR REPLACE VIEW `app`.`vw_x` AS\nSELECT 1;',
                  [("app.vw_x", "vw_x")]),
    "legacy_unquoted": ('CREATE OR REPLACE VIEW public.vw_x AS\nSELECT 1;',
                        [("public.vw_x", "vw_x")]),
}


def test_extract_view_names_per_platform():
    for plat, (ddl, expected) in _CASES.items():
        got = ssd.extract_view_names(ddl)
        assert got == expected, f"{plat}: {got!r} != {expected!r}"
        # Bare names must never carry a stray quote/backtick (the original bug).
        for _q, bare in got:
            assert '"' not in bare and "`" not in bare and bare


def test_extract_view_names_multi_statement():
    ddl = (
        "-- Output dataset: PUBLIC.TEAMS\n"
        'CREATE OR REPLACE VIEW "DB"."PUBLIC"."vw_teams" COPY GRANTS AS SELECT 1;\n\n'
        "-- Output dataset: PUBLIC.PLAYERS\n"
        'CREATE OR REPLACE VIEW "DB"."PUBLIC"."vw_players" COPY GRANTS AS SELECT 2;'
    )
    assert [b for _, b in ssd.extract_view_names(ddl)] == ["vw_teams", "vw_players"]


def test_comment_lines_not_scooped():
    # A `-- CREATE VIEW ...` comment must not be parsed as a real statement.
    ddl = '-- this CREATE VIEW note is a comment\nCREATE OR REPLACE VIEW "s"."vw_real" AS SELECT 1;'
    assert [b for _, b in ssd.extract_view_names(ddl)] == ["vw_real"]
