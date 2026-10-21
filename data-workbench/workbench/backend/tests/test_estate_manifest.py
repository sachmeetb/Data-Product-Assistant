"""Offline estate manifest — the client↔server contract + pure converters.

Pure/offline: no DB, Neo4j, or MCP needed. Guards the properties the offline-
extraction plan depends on: fail-closed validation (a bad entry rejects the whole
doc), the pure ``to_relation_dicts`` converter emitting the EXACT live-scan dict
shape, derived-not-trusted classification, FK consolidation, and the preview
summary (redaction + PII visibility).
"""
import pytest

from workbench.backend import estate_manifest as em
from workbench.backend.estate_manifest import EstateManifestValidationError


def _valid_manifest_dict():
    return {
        "manifest_version": "1",
        "kind": "estate",
        "platform": "postgres",
        "catalog": "analytics",
        "generated_at": "2026-08-27T00:00:00Z",
        "tool_version": "1",
        "extraction": {
            "metadata": True, "volumetrics": True, "profiling": True,
            "values_included": True, "code_assets": False,
            "redaction": {"pii_redacted": 1, "cardinality_capped": 0},
        },
        "relations": [
            {
                "schema": "sales",
                "table": "orders",
                "relation_kind": "table",
                "row_count": 128934,
                "row_count_is_estimate": False,
                "size_bytes": 20447232,
                "last_modified": "2026-08-20T00:00:00Z",
                "comment": "Order header",
                "columns": [
                    {"name": "customer_id", "data_type": "integer", "nullable": False,
                     "ordinal": 1, "primary_key": False},
                    {"name": "order_status", "data_type": "varchar", "nullable": False,
                     "ordinal": 4, "profile": {
                         "null_count": 0, "null_rate": 0.0, "distinct_count": 5,
                         "top_values": [{"value": "OPEN", "count": 40120, "frequency": 0.311}],
                     }},
                    {"name": "customer_email", "data_type": "varchar", "nullable": True,
                     "ordinal": 5, "profile": {
                         "null_count": 3, "distinct_count": 900,
                         "redacted": True, "redaction_reason": "pii",
                     }},
                ],
                "foreign_keys": [
                    {"from_column": "customer_id", "to_schema": "sales",
                     "to_table": "customers", "to_column": "customer_id"},
                ],
            },
        ],
        "code_assets": [],
    }


# ── validation (fail-closed) ─────────────────────────────────────────────────

def test_valid_manifest_parses():
    m = em.parse_manifest(_valid_manifest_dict())
    assert m.platform == "postgres"
    assert m.kind == "estate"
    assert m.db_segment == "analytics"
    assert len(m.relations) == 1
    assert m.relations[0].schema_name == "sales"
    assert m.relations[0].columns[1].profile.top_values[0].value == "OPEN"


def test_unsupported_manifest_version_rejected():
    d = _valid_manifest_dict()
    d["manifest_version"] = "99"
    with pytest.raises(EstateManifestValidationError):
        em.parse_manifest(d)


def test_unsupported_platform_rejected():
    d = _valid_manifest_dict()
    d["platform"] = "teradata"
    with pytest.raises(EstateManifestValidationError):
        em.parse_manifest(d)


def test_bad_kind_rejected():
    d = _valid_manifest_dict()
    d["kind"] = "garbage"
    with pytest.raises(EstateManifestValidationError):
        em.parse_manifest(d)


def test_empty_column_name_rejects_whole_doc():
    d = _valid_manifest_dict()
    d["relations"][0]["columns"][0]["name"] = "  "
    with pytest.raises(EstateManifestValidationError):
        em.parse_manifest(d)


def test_bad_relation_kind_rejected():
    d = _valid_manifest_dict()
    d["relations"][0]["relation_kind"] = "sequence"
    with pytest.raises(EstateManifestValidationError):
        em.parse_manifest(d)


def test_missing_table_name_rejected():
    d = _valid_manifest_dict()
    d["relations"][0]["table"] = ""
    with pytest.raises(EstateManifestValidationError):
        em.parse_manifest(d)


def test_load_manifest_bad_yaml():
    with pytest.raises(EstateManifestValidationError):
        em.load_manifest("::: not: yaml: [")


def test_load_manifest_empty():
    with pytest.raises(EstateManifestValidationError):
        em.load_manifest("")


def test_load_manifest_roundtrip():
    import yaml
    raw = yaml.safe_dump(_valid_manifest_dict())
    m = em.load_manifest(raw)
    assert m.platform == "postgres"


# ── pure converter: exact live-scan shape parity ─────────────────────────────

def test_to_relation_dicts_shape_parity():
    m = em.parse_manifest(_valid_manifest_dict())
    # inject a stub classifier so the test doesn't depend on estate.classify_column
    rels = em.to_relation_dicts(m, classify=lambda name: "stub")
    assert len(rels) == 1
    r = rels[0]
    # exactly the keys estate.write_scan_snapshot / run_scan use
    assert set(r.keys()) == {
        "database", "schema", "table", "relation_kind", "row_count",
        "size_bytes", "last_modified", "num_files", "row_count_is_estimate",
        "columns",
    }
    assert r["database"] == "analytics"
    assert r["schema"] == "sales"
    assert r["table"] == "orders"
    assert r["relation_kind"] == "table"
    assert r["row_count"] == 128934
    assert r["row_count_is_estimate"] is False
    col = r["columns"][0]
    assert set(col.keys()) == {"name", "data_type", "nullable", "ordinal", "classification"}
    assert col["classification"] == "stub"


def test_classification_is_derived_not_trusted():
    """A client-forged 'public' classification must be ignored — the importer
    derives it from the name via estate.classify_column."""
    d = _valid_manifest_dict()
    # attacker tries to smuggle a classification onto a PII-named column
    d["relations"][0]["columns"][2]["classification"] = "public"
    m = em.parse_manifest(d)
    rels = em.to_relation_dicts(m)  # real classifier
    email_col = next(c for c in rels[0]["columns"] if c["name"] == "customer_email")
    assert email_col["classification"] == "pii"


def test_two_level_platform_empty_db_segment():
    d = _valid_manifest_dict()
    d["catalog"] = None
    m = em.parse_manifest(d)
    rels = em.to_relation_dicts(m, classify=lambda n: "internal")
    assert rels[0]["database"] == ""


# ── FK consolidation ─────────────────────────────────────────────────────────

def test_to_fk_tuples_consolidates():
    d = _valid_manifest_dict()
    d["relations"][0]["foreign_keys"] = [
        {"from_column": "a_id", "to_schema": "sales", "to_table": "dim", "to_column": "id"},
        {"from_column": "b_id", "to_schema": "sales", "to_table": "dim", "to_column": "id2"},
    ]
    m = em.parse_manifest(d)
    pairs = em.to_fk_tuples(m, estate_id=1, source_id=2)
    assert len(pairs) == 1  # both FK cols fold onto ONE edge (same src→tgt pair)
    src_uri, tgt_uri, cols, refs = pairs[0]
    assert cols == ["a_id", "b_id"]
    assert refs == ["id", "id2"]
    assert "estatedataset:1:2:analytics.sales.orders" == src_uri
    assert "estatedataset:1:2:analytics.sales.dim" == tgt_uri


# ── preview summary (no side effects) ────────────────────────────────────────

def test_summarize_manifest():
    m = em.parse_manifest(_valid_manifest_dict())
    s = em.summarize_manifest(m)
    assert s["counts"]["relations"] == 1
    assert s["counts"]["columns"] == 3
    assert s["counts"]["fk_edges"] == 1
    assert s["counts"]["profiled_columns"] == 2
    assert s["counts"]["redacted_columns"] == 1
    assert s["depth"] == "profiled"
    # customer_email is PII by name heuristic
    assert any("customer_email" in c for c in s["pii_columns"])
    assert s["counts"]["pii_columns"] >= 1


def test_summarize_metadata_only_depth():
    d = _valid_manifest_dict()
    for rel in d["relations"]:
        for c in rel["columns"]:
            c.pop("profile", None)
    d["extraction"]["profiling"] = False
    m = em.parse_manifest(d)
    s = em.summarize_manifest(m)
    assert s["depth"] == "metadata"
    assert s["counts"]["profiled_columns"] == 0


def test_to_discovery_docs_shape():
    """Greenfield (Phase 2): the discovery-doc converter emits the exact shape the
    data-discovery loader consumes — schema/table/columns(type,nullable,ordinal,
    comment) + primary_key + foreign_keys."""
    m = em.parse_manifest(_valid_manifest_dict())
    docs = em.to_discovery_docs(m)
    assert set(docs) == {"sales__orders.yaml"}
    doc = docs["sales__orders.yaml"]
    assert doc["schema"] == "sales" and doc["table"] == "orders"
    assert doc["comment"] == "Order header"
    # customer_id is a PK-less col here; the dict has type from data_type
    col = doc["columns"][0]
    assert set(col) == {"name", "ordinal", "type", "character_maximum_length",
                        "numeric_precision", "numeric_scale", "nullable", "default", "comment"}
    assert col["type"] == "integer"
    # FK maps to the loader's shape
    fk = doc["foreign_keys"][0]
    assert fk["columns"] == ["customer_id"]
    assert fk["referenced_table"] == "customers"
    assert fk["referenced_columns"] == ["customer_id"]


def test_to_discovery_docs_primary_key():
    d = _valid_manifest_dict()
    d["relations"][0]["columns"][0]["primary_key"] = True  # customer_id
    m = em.parse_manifest(d)
    doc = em.to_discovery_docs(m)["sales__orders.yaml"]
    assert doc["primary_key"]["columns"] == ["customer_id"]


def test_to_profile_docs_shape_and_redaction():
    """Only profiled columns are emitted; redacted value-bearing fields are dropped
    (the loader only writes non-null metrics), safe enumerations survive."""
    m = em.parse_manifest(_valid_manifest_dict())
    docs = em.to_profile_docs(m)
    assert set(docs) == {"sales__orders__profile.yaml"}
    doc = docs["sales__orders__profile.yaml"]
    by_name = {c["name"]: c for c in doc["columns"]}
    # customer_id has no profile → absent
    assert "customer_id" not in by_name
    # order_status: safe, has top_values
    assert by_name["order_status"]["distinct_count"] == 5
    assert by_name["order_status"]["top_values"][0]["value"] == "OPEN"
    # customer_email: redacted → counts kept, no top_values
    assert by_name["customer_email"]["null_count"] == 3
    assert "top_values" not in by_name["customer_email"]


def test_to_profile_docs_empty_when_no_profiles():
    d = _valid_manifest_dict()
    for c in d["relations"][0]["columns"]:
        c.pop("profile", None)
    m = em.parse_manifest(d)
    assert em.to_profile_docs(m) == {}


def test_code_asset_summaries():
    d = _valid_manifest_dict()
    d["code_assets"] = [
        {"name": "refresh_orders", "asset_kind": "procedure", "namespace": "sales",
         "language": "sql", "definition_preview": "BEGIN ...", "depends_on": ["x"]},
    ]
    m = em.parse_manifest(d)
    summaries = em.to_code_asset_summaries(m)
    assert len(summaries) == 1
    assert summaries[0].name == "refresh_orders"
    assert summaries[0].namespace.parts == ["sales"]
    assert summaries[0].depends_on == ["x"]
