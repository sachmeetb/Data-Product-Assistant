"""Blueprint Library tests (Neo4j-free).

The graph-touching save/read/publish paths are proven by the live end-to-end
smoke; here we assert everything that runs without Neo4j:

- the **bidirectional** FeasibilitySpec ⇄ ODCS field map round-trips exactly
  (modulo documented lossy fields) over the whole corpus;
- ``canonicalize_template`` idempotence + preservation of the template-only
  fields the ODCS canonicaliser would otherwise sweep into ``extras``;
- the ODCS→C re-derivation fallback (no ``customProperties.feasibility``);
- the Library→C corpus generator writes a corpus that ``load_corpus`` accepts
  fail-closed, with a consistent ``index.yaml``, a bumped ``corpus_version``,
  and drafts excluded;
- every committed seed file projects to a valid feasibility spec.
"""
from __future__ import annotations

import yaml

from workbench.backend import feasibility_map as fm
from workbench.backend import feasibility_spec as fs
from workbench.backend import template_corpus
from workbench.backend.template_store import canonicalize_template


def _dump(model) -> dict:
    d = model.model_dump()
    d.pop("corpus_version", None)  # loader-stamped, not authored
    return d


# ── field map: bidirectional round trip ──────────────────────────────────────

def test_field_map_roundtrip_full_corpus():
    specs = fs.load_corpus()
    assert specs, "corpus must be non-empty"
    for s in specs:
        odcs = fm.feasibility_spec_to_odcs(s)
        back = fs.parse_spec(fm.odcs_to_feasibility_spec(odcs))  # fail-closed
        assert _dump(back) == _dump(s), f"round trip diverged for {s.spec_id}"


def test_field_map_id_and_source_spec_id():
    s = fs.load_corpus()[0]
    odcs = fm.feasibility_spec_to_odcs(s)
    assert odcs["id"] == fm.template_id(s.domain, s.spec_id)
    assert odcs["id"].startswith("template:")
    # the ORIGINAL (un-slugged) spec_id is preserved for exact recovery
    assert odcs["sourceSpecId"] == s.spec_id
    assert fm.odcs_to_feasibility_spec(odcs)["spec_id"] == s.spec_id


def test_lossy_history_mapping_explicit():
    # current ↔ latest_only is the one documented lossy enum bridge.
    for s in fs.load_corpus():
        odcs = fm.feasibility_spec_to_odcs(s)
        scd = (odcs["schema"][0].get("transform") or {}).get("scd_policy") or {}
        if s.freshness.history.value == "current":
            assert scd.get("type") == "latest_only"
        else:
            assert scd.get("type") == s.freshness.history.value


def test_classification_public_restricted_roundtrip():
    # public / restricted have no ODCS sensitivity peer but round-trip via the
    # verbatim `classification` property string.
    for verbatim in ("public", "restricted", "pii", "confidential"):
        raw = {
            "spec_id": "t", "name": "T", "domain": "D",
            "attributes": [{"name": "c", "classification": verbatim, "is_key": True}],
        }
        s = fs.parse_spec(raw)
        odcs = fm.feasibility_spec_to_odcs(s)
        prop = odcs["schema"][0]["properties"][0]
        assert prop["classification"] == verbatim
        back = fs.parse_spec(fm.odcs_to_feasibility_spec(odcs))
        assert back.attributes[0].classification.value == verbatim


# ── ODCS→C re-derivation fallback (no customProperties.feasibility) ──────────

def test_reverse_derives_without_cp_namespace():
    odcs = {
        "id": "template:hr:x", "name": "X", "domain": "HR", "sourceSpecId": "x",
        "productKind": "source",
        "schema": [{
            "name": "X", "physicalName": "x",
            "properties": [
                {"name": "employee_id", "physicalType": "bigint",
                 "primaryKey": True, "required": True, "sensitivity": "pii"},
                {"name": "salary", "physicalType": "numeric", "required": True},
            ],
        }],
        # deliberately NO customProperties.feasibility
    }
    raw = fm.odcs_to_feasibility_spec(odcs)
    spec = fs.parse_spec(raw)                      # must still validate
    assert spec.grain.keys == ["employee_id"]      # PK → grain key
    assert spec.attributes[0].is_key
    # sensitivity=pii → classification derived as pii
    assert spec.attributes[0].classification.value == "pii"


# ── canonicalize_template ────────────────────────────────────────────────────

def test_canonicalize_template_idempotent_and_preserves_template_fields():
    s = fs.load_corpus()[0]
    odcs = fm.feasibility_spec_to_odcs(s)
    once = canonicalize_template(odcs)
    twice = canonicalize_template(once)
    assert once == twice, "canonicalize_template must be idempotent"
    # productKind + sourceSpecId survive (the canonicaliser sweeps them to extras)
    assert once["productKind"] == s.product_kind.value
    assert once["sourceSpecId"] == s.spec_id
    assert "extras" not in once or "productKind" not in (once.get("extras") or {})


def test_canonicalize_strips_template_meta_keys():
    raw = {
        "id": "template:x:y", "name": "Y", "domain": "X",
        "isTemplate": True, "templateStatus": "published",
        "templateOrigin": "seed", "clonedFrom": "template:a:b",
        "schema": [{"name": "Y", "physicalName": "y", "properties": []}],
    }
    canon = canonicalize_template(raw)
    for k in ("isTemplate", "templateStatus", "templateOrigin", "clonedFrom"):
        assert k not in canon
        assert k not in (canon.get("extras") or {})


# ── Library → feasibility corpus generator ───────────────────────────────────

def _make_published_template(spec: fs.FeasibilitySpec) -> dict:
    odcs = canonicalize_template(fm.feasibility_spec_to_odcs(spec))
    odcs["isTemplate"] = True
    odcs["templateStatus"] = "published"
    return odcs


def test_regenerate_corpus_from_library(monkeypatch, tmp_path):
    # Read the REAL corpus BEFORE redirecting the corpus paths to tmp.
    all_specs = fs.load_corpus()
    src_specs = all_specs[:6]
    draft_spec = all_specs[6]

    corpus_dir = tmp_path / "reference"
    monkeypatch.setattr(fs, "CORPUS_DIR", corpus_dir)
    monkeypatch.setattr(fs, "CORPUS_MANIFEST", corpus_dir / "corpus.yaml")
    monkeypatch.setattr(fs, "INDEX_FILE", corpus_dir / "index.yaml")

    published = {fm.template_id(s.domain, s.spec_id): _make_published_template(s)
                 for s in src_specs}
    draft_id = fm.template_id(draft_spec.domain, draft_spec.spec_id)

    rows = [
        {"id": cid, "status": "published"} for cid in published
    ] + [{"id": draft_id, "status": "draft"}]

    monkeypatch.setattr(template_corpus, "list_templates_raw", lambda session: rows)
    monkeypatch.setattr(
        template_corpus, "_read_template_from_graph",
        lambda cid, session, version=None: published.get(cid),
    )

    result = template_corpus.regenerate_feasibility_corpus(session=None)

    # draft excluded; every published spec written.
    assert result["written_count"] == len(published)
    assert result["corpus_version"] != "0"          # bumped off the empty tmp

    # the freshly-written corpus loads fail-closed and excludes the draft.
    loaded = fs.load_corpus()
    assert len(loaded) == len(published)
    assert draft_spec.spec_id not in {s.spec_id for s in loaded}

    # index.yaml consistent with the written spec files.
    index = yaml.safe_load((corpus_dir / "index.yaml").read_text())["specs"]
    assert {e["spec_id"] for e in index} == {s.spec_id for s in loaded}
    for e in index:
        assert e["total_count"] >= e["required_count"] >= 0


# ── runtime graph loader (retires the file corpus at runtime) ────────────────

def test_load_specs_from_graph(monkeypatch):
    """The runtime loader projects PUBLISHED templates to FeasibilitySpec live from
    the graph, filters by domain, and SKIPS malformed / attribute-less templates
    instead of failing closed (a single bad template must not break the list/scan);
    drafts are excluded. Neo4j-free via the same mocking as the regen test."""
    all_specs = fs.load_corpus()
    valid = all_specs[:2]
    published = {fm.template_id(s.domain, s.spec_id): _make_published_template(s)
                 for s in valid}

    # A published-but-broken template: has attributes but an empty domain → the
    # projected spec fails validation (skipped, not raised).
    malformed_id = "template:cards:malformed"
    published[malformed_id] = {
        "id": malformed_id, "name": "Malformed", "domain": "",
        "sourceSpecId": "malformed_spec", "isTemplate": True, "templateStatus": "published",
        "schema": [{"name": "M", "physicalName": "m",
                    "properties": [{"name": "some_col", "physicalType": "varchar"}]}],
    }
    # A published-but-attribute-less template (no columns → not a gradeable spec).
    attrless_id = "template:cards:attrless"
    published[attrless_id] = {
        "id": attrless_id, "name": "Attrless", "domain": "Cards",
        "sourceSpecId": "attrless_spec", "isTemplate": True, "templateStatus": "published",
        "schema": [{"name": "A", "physicalName": "a", "properties": []}],
    }
    # A draft — excluded regardless of validity.
    draft_spec = all_specs[2]
    draft_id = fm.template_id(draft_spec.domain, draft_spec.spec_id)
    draft_odcs = _make_published_template(draft_spec)
    draft_odcs["templateStatus"] = "draft"

    rows = ([{"id": cid, "status": "published",
              "domain": published[cid].get("domain", "")} for cid in published]
            + [{"id": draft_id, "status": "draft", "domain": draft_spec.domain}])
    reads = {**published, draft_id: draft_odcs}

    monkeypatch.setattr(template_corpus, "list_templates_raw", lambda session: rows)
    monkeypatch.setattr(
        template_corpus, "_read_template_from_graph",
        lambda cid, session, version=None: reads.get(cid),
    )

    # (a) valid published specs project; (c) malformed + attribute-less skipped;
    # (d) draft excluded.
    loaded = template_corpus.load_specs_from_graph(session=None)
    got = {s.spec_id for s in loaded}
    assert got == {s.spec_id for s in valid}
    assert draft_spec.spec_id not in got
    # marker stamped in place of the retired file corpus_version.
    assert all(s.corpus_version == template_corpus.GRAPH_CORPUS_MARKER for s in loaded)

    # (b) domain filter narrows to the matching domain.
    one = valid[0]
    dom = template_corpus.load_specs_from_graph(session=None, domain=one.domain)
    assert one.spec_id in {s.spec_id for s in dom}
    assert all(fs.slug(s.domain) == fs.slug(one.domain) for s in dom)

    # the lightweight index + domain list read the same live source.
    idx = template_corpus.load_index_from_graph(session=None)
    assert {e["spec_id"] for e in idx} == got
    assert set(template_corpus.list_domains_from_graph(session=None)) == {s.domain for s in valid}


def test_bump_version():
    assert template_corpus._bump_version("1.0") == "1.1"
    assert template_corpus._bump_version("0") == "0.1"
    assert template_corpus._bump_version("2.9") == "2.10"
    assert template_corpus._bump_version("") == "0.1"


# ── design completeness ──────────────────────────────────────────────────────

def test_completeness_flags_missing_design_fields():
    from workbench.backend.routers.templates import _template_completeness
    s = fs.load_corpus()[0]
    spec = canonicalize_template(fm.feasibility_spec_to_odcs(s))
    comp = _template_completeness(spec)
    assert comp["band"] in ("green", "amber", "red")
    assert 0 <= comp["completeness"] <= 100
    ids = {c["id"] for c in comp["design"]["checklist"]}
    assert {"product_description", "primary_key", "field_descriptions"} <= ids
    # a corpus spec has no operational fields → all informational-empty, but
    # that must NOT drag the design completeness score.
    assert all(o["status"] in ("set", "empty") for o in comp["operational"]["checklist"])

    # an empty template surfaces the core gaps.
    blank = {"schema": []}
    blank_comp = _template_completeness(blank)
    assert blank_comp["band"] == "red"
    assert "has_columns" in blank_comp["design"]["missing"]


# ── PO MCP tools registered ──────────────────────────────────────────────────

def test_po_mcp_template_tools_registered():
    import re as _re
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "po_mcp_server.py").read_text()
    names = set(_re.findall(r"@po_mcp\.tool\(\)\n(?:async )?def (\w+)", src))
    expected = {
        "list_templates", "get_template", "get_template_completeness",
        "create_template", "import_template", "clone_template",
        "save_template", "publish_template", "export_template",
    }
    assert expected <= names, f"missing PO template MCP tools: {expected - names}"


# ── isolation boundary ───────────────────────────────────────────────────────

def test_template_isolation_guards():
    # The template save path must NEVER link a template to a project or run the
    # ODCS→dprod materialisation (that's what keeps templates out of marketplace
    # / lineage / feasibility by construction).
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "template_store.py").read_text()
    # No CALL to either (the docstring may mention the names in prose).
    assert "link_contract_to_project(" not in src
    assert "_generate_dprod(" not in src

    # Belt-and-suspenders: both functions refuse a `template:` id BEFORE any
    # Neo4j access, so a stray caller can't materialise/link a template (these
    # run without a graph — project arg is never dereferenced).
    from workbench.backend.graph_ops import link_contract_to_project
    from workbench.backend.routers.odcs import _generate_dprod
    assert link_contract_to_project(None, "template:cards:x") is None  # type: ignore[arg-type]
    out = _generate_dprod("template:cards:x", None)  # type: ignore[arg-type]
    assert out.get("skipped") == "product_template"


# ── committed seed files are valid ───────────────────────────────────────────

def test_committed_seed_files_project_to_valid_specs():
    seed_dir = template_corpus.TEMPLATE_SEED_DIR
    files = list(seed_dir.rglob("*.yaml"))
    assert files, "expected committed seed files under playbook/template_seed/"
    for path in files:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        assert raw.get("id", "").startswith("template:"), path.name
        canon = canonicalize_template(raw)
        projected = fm.odcs_to_feasibility_spec(canon)
        if projected.get("attributes"):
            fs.parse_spec(projected)  # fail-closed
