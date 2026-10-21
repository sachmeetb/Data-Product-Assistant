"""Tier 7: the normalized `searchName` identity property.

Covers:
- discovery loader writes searchName = toLower(name) on :Dataset + :Column
- odcs.py dprod builder writes searchName on :DProdColumn
- the lookup source-resolution reads are case-insensitive via searchName
  (coalesce fallback to toLower(name) for un-backfilled graphs)
- the backfill script targets the three identity labels
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from workbench.backend.config import BASE_DIR


def _load(mod_name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(mod_name, BASE_DIR / rel_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── discovery loader write site ────────────────────────────────────────────────

def test_discovery_loader_writes_searchname_lowercased():
    gc = _load("gen_cypher_under_test",
               "workbench-skills/skills/data-discovery-to-dcat-neo4j/scripts/generate_cypher.py")
    tables = [{
        "schema": "HR_Core", "table": "Employee",
        "columns": [{"name": "Employee_ID", "type": "int", "ordinal": 1}],
    }]
    lines, _stats = gc.build_cypher(tables, project_code="p1")
    text = "\n".join(lines)
    # Dataset + Column both get searchName = toLower(name).
    assert "ds.searchName = 'employee'" in text
    assert "col.searchName = 'employee_id'" in text
    # The case-preserving name is untouched.
    assert "ds.name = 'Employee'" in text
    assert "col.name = 'Employee_ID'" in text


# ── dprod builder write site ───────────────────────────────────────────────────

def test_odcs_dprod_column_sets_searchname():
    from workbench.backend.routers.odcs import DPROD_CREATE_COLUMN, DPROD_CREATE_DATASET
    assert "pc.searchName = toLower(p.name)" in DPROD_CREATE_COLUMN
    # :DProdOutputDataset is intentionally NOT given searchName (its join key is
    # physicalName, matched case-insensitively inline) — keep the property to the
    # three identity labels.
    assert "searchName" not in DPROD_CREATE_DATASET


# ── read sites: case-insensitive lookup resolution ─────────────────────────────

def test_lookup_via_reads_are_case_insensitive():
    from workbench.backend import lookup_via as lv
    for q in (lv._RESOLVE_CATALOG_COLUMN, lv._RESOLVE_DPROD_COLUMN):
        assert "searchName" in q and "toLower(" in q
    # coalesce fallback so un-backfilled graphs still resolve.
    assert "coalesce(col.searchName, toLower(col.name))" in lv._RESOLVE_CATALOG_COLUMN
    assert "coalesce(c.searchName, toLower(c.name))" in lv._RESOLVE_DPROD_COLUMN


def test_write_mappings_lookup_matches_backend_twin():
    wm = _load("write_mappings_under_test",
               "workbench-skills/skills/data-mapping-neo4j/scripts/write_mappings.py")
    from workbench.backend import lookup_via as lv
    # The skill and the backend reconcile must stay in sync (CLAUDE.md contract).
    assert "coalesce(ds.searchName, toLower(ds.name))" in wm._RESOLVE_CATALOG_LOOKUP
    assert "coalesce(col.searchName, toLower(col.name))" in wm._RESOLVE_CATALOG_LOOKUP
    assert "coalesce(c.searchName, toLower(c.name))" in wm._RESOLVE_DPROD_LOOKUP
    # Same normalization on both sides of the pair.
    assert ("coalesce(col.searchName, toLower(col.name))"
            in lv._RESOLVE_CATALOG_COLUMN)


# ── backfill script ────────────────────────────────────────────────────────────

def test_backfill_targets_three_identity_labels():
    bf = _load("backfill_search_name_under_test", "scripts/backfill_search_name.py")
    assert bf._LABELS == ["Dataset", "Column", "DProdColumn"]
