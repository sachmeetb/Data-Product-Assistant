"""Core invariants. Each test corresponds to a defect in a predecessor product."""

import pytest

from bronze.envelope import apply_envelope, envelope_for, missing_envelope, required_names
from core.artifacts.graph import ArtifactGraph, ArtifactId, Staleness, downstream
from core.flow.graph import Edge, FlowGraph, Node, NodeKind, NoTransition, always
from core.model.provenance import Origin, Provenance
from core.model.tree import Attribute, EntityType, Layer, Table
from core.model.types import CanonicalType, UnknownTypeError, normalize, register_target, render
from pipelines.ir import (
    DQRule, Format, PipelineSpec, Severity, Source, SourceKind, Step, StepKind,
    Target, WriteMode,
)


# --- types -------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("DOUBLE", CanonicalType.FLOAT),      # Spark type found in GOLD's frameworks
        ("FLOAT64", CanonicalType.FLOAT),
        ("INT64", CanonicalType.INT),
        ("NUMERIC", CanonicalType.DECIMAL),
        ("DECIMAL(18,2)", CanonicalType.DECIMAL),
        ("ARRAY<STRING>", CanonicalType.ARRAY),
        ("boolean", CanonicalType.BOOL),
    ],
)
def test_normalize(raw, expected):
    assert normalize(raw) is expected


def test_double_renders_to_valid_bigquery():
    """DOUBLE is not a BigQuery type; FLOAT64 is."""
    assert render(normalize("DOUBLE")) == "FLOAT64"


def test_unknown_type_raises_rather_than_defaulting():
    with pytest.raises(UnknownTypeError):
        normalize("WIDGET")


def test_register_target_requires_full_coverage():
    with pytest.raises(ValueError, match="missing mappings"):
        register_target("partial", {CanonicalType.STRING: "TEXT"})


# --- merge policy ------------------------------------------------------------

def _attr(name, ctype, origin=Origin.LLM, locked=False, description=""):
    return Attribute(
        name=name,
        canonical_type=ctype,
        description=description,
        provenance=Provenance(origin=origin, locked=locked),
    )


def _table(*attrs):
    return Table(
        name="slv_txn", layer=Layer.SILVER, entity_type=EntityType.EVENT,
        attributes=list(attrs),
    )


def test_human_edit_survives_regeneration():
    existing = _table(_attr("amount", CanonicalType.DECIMAL, Origin.HUMAN, locked=True))
    incoming = _table(_attr("amount", CanonicalType.FLOAT))

    report = existing.merge(incoming)

    assert existing.attribute("amount").canonical_type is CanonicalType.DECIMAL
    assert report.preserved == ["slv_txn/amount"]
    assert len(report.withheld) == 1
    assert report.withheld[0].proposed["type"] == "FLOAT"


def test_live_catalog_attribute_is_protected():
    existing = _table(_attr("branch_code", CanonicalType.STRING, Origin.LIVE_CATALOG))
    incoming = _table(_attr("branch_code", CanonicalType.INT))

    existing.merge(incoming)

    assert existing.attribute("branch_code").canonical_type is CanonicalType.STRING


def test_protected_attribute_not_dropped_when_absent_from_regeneration():
    existing = _table(_attr("amount", CanonicalType.DECIMAL, Origin.HUMAN, locked=True))
    report = existing.merge(_table())

    assert existing.has("amount")
    assert report.removed == []


def test_unprotected_attribute_is_replaced_and_removed():
    existing = _table(
        _attr("channel", CanonicalType.STRING),
        _attr("stale", CanonicalType.STRING),
    )
    incoming = _table(_attr("channel", CanonicalType.STRING, description="described"))

    report = existing.merge(incoming)

    assert report.replaced == ["slv_txn/channel"]
    assert report.removed == ["slv_txn/stale"]
    assert not existing.has("stale")


def test_new_attributes_are_added():
    existing = _table()
    report = existing.merge(_table(_attr("event_ts", CanonicalType.TIMESTAMP)))

    assert report.added == ["slv_txn/event_ts"]
    assert existing.has("event_ts")


def test_human_touch_locks_provenance():
    p = Provenance(origin=Origin.LLM)
    assert not p.is_protected
    assert p.touched_by_human("sachmeet").is_protected


# --- flow graph --------------------------------------------------------------

def _graph():
    return FlowGraph(
        nodes=[
            Node("define", NodeKind.AGENT, produces="requirement"),
            Node("gate", NodeKind.GATE),
            Node("design", NodeKind.AGENT, consumes=["requirement"]),
            Node("done", NodeKind.TERMINAL),
        ],
        edges=[
            Edge("define", "gate", always),
            Edge("gate", "design", lambda s: s.get("confirmed") is True),
            Edge("design", "done", always),
        ],
        entry="define",
    )


def test_valid_graph_has_no_problems():
    assert _graph().validate() == []


def test_guard_selects_transition():
    assert _graph().next("gate", {"confirmed": True}).name == "design"


def test_failed_guard_raises_instead_of_falling_through():
    with pytest.raises(NoTransition):
        _graph().next("gate", {"confirmed": False})


def test_terminal_state_refuses_reentry():
    """SILVER's stage 3 was unguarded: any message after completion re-ran it."""
    with pytest.raises(NoTransition, match="terminal"):
        _graph().next("done", {})


def test_validate_catches_unreachable_node():
    g = FlowGraph(
        nodes=[Node("a", NodeKind.AGENT), Node("orphan", NodeKind.AGENT),
               Node("b", NodeKind.TERMINAL)],
        edges=[Edge("a", "b", always)],
        entry="a",
    )
    problems = g.validate()
    assert any("unreachable" in p for p in problems)
    assert any("no outgoing edges" in p for p in problems)


def test_validate_catches_unsatisfied_consumes():
    g = FlowGraph(
        nodes=[Node("a", NodeKind.AGENT, consumes=["nothing_makes_this"]),
               Node("b", NodeKind.TERMINAL)],
        edges=[Edge("a", "b", always)],
        entry="a",
    )
    assert any("which no node produces" in p for p in g.validate())


# --- artifact DAG ------------------------------------------------------------

def test_edit_invalidates_only_downstream():
    affected = downstream(ArtifactId.DESIGN_TREE)

    assert ArtifactId.STTM in affected
    assert ArtifactId.PIPELINE_IR in affected
    assert ArtifactId.PUBLISHED in affected
    assert ArtifactId.DISCOVERY not in affected
    assert ArtifactId.REQUIREMENT not in affected


def test_requirement_change_invalidates_everything_after_it():
    affected = downstream(ArtifactId.REQUIREMENT)
    assert ArtifactId.DISCOVERY in affected
    assert ArtifactId.DESIGN_TREE in affected


def test_staleness_cascades_and_spares_siblings():
    g = ArtifactGraph()
    built = [
        ArtifactId.REQUIREMENT, ArtifactId.CLASSIFICATION, ArtifactId.BRONZE_PROFILE,
        ArtifactId.BRONZE_TABLES, ArtifactId.DISCOVERY, ArtifactId.CHALLENGE,
        ArtifactId.DESIGN_TREE, ArtifactId.GOLD_ER, ArtifactId.STTM,
    ]
    for a in built:
        g.mark_built(a)

    stale = g.mark_changed(ArtifactId.DESIGN_TREE)

    assert ArtifactId.STTM in stale
    assert g.state(ArtifactId.DISCOVERY).staleness is Staleness.FRESH
    assert g.state(ArtifactId.STTM).stale_because == [ArtifactId.DESIGN_TREE]


def test_version_increments_on_build():
    g = ArtifactGraph()
    assert g.state(ArtifactId.REQUIREMENT).version == 0
    g.mark_built(ArtifactId.REQUIREMENT)
    g.mark_built(ArtifactId.REQUIREMENT)
    assert g.state(ArtifactId.REQUIREMENT).version == 2


def test_buildable_respects_upstream_freshness():
    g = ArtifactGraph()
    assert ArtifactId.REQUIREMENT in g.buildable()
    assert ArtifactId.STTM not in g.buildable()


# --- bronze envelope ---------------------------------------------------------

@pytest.mark.parametrize("layer", [Layer.BRONZE, Layer.SILVER, Layer.GOLD])
def test_envelope_check_and_emitter_share_one_source(layer):
    """SILVER's GR001 rule and its emitter disagreed; that is now impossible."""
    assert required_names(layer) == {a.name for a in envelope_for(layer)}


def test_apply_envelope_is_idempotent():
    t = Table(name="brz_raw", layer=Layer.BRONZE, entity_type=EntityType.RAW)
    first = apply_envelope(t)

    assert first
    assert not missing_envelope(t)
    assert apply_envelope(t) == []


def test_bronze_envelope_supports_replay_and_quarantine():
    names = required_names(Layer.BRONZE)
    assert "source_file_reference" in names
    assert "dq_status" in names
    assert "bronze_ingest_ts" in names


# --- pipeline IR -------------------------------------------------------------

def _spec():
    return PipelineSpec(
        id="p1", name="source to bronze",
        sources=[Source(id="src", kind=SourceKind.OBJECT_STORE,
                        format=Format.CSV, location="gs://b/*.csv")],
        steps=[Step(id="cast", kind=StepKind.CAST, inputs=["src"], output="typed")],
        targets=[Target(id="brz", layer=Layer.BRONZE, dataset="bronze",
                        table="brz_txn", write_mode=WriteMode.APPEND)],
        dq=[DQRule(id="nn", target="typed", column="amount", check="not_null",
                   severity=Severity.QUARANTINE)],
    )


def test_valid_ir_passes():
    assert _spec().validate() == []


def test_ir_catches_step_consuming_unproduced_input():
    bad = PipelineSpec(
        id="p", name="bad",
        steps=[Step(id="s", kind=StepKind.CAST, inputs=["ghost"], output="x")],
    )
    problems = bad.validate()
    assert any("before it is produced" in p for p in problems)
    assert any("no targets" in p for p in problems)


def test_ir_catches_duplicate_ids():
    dup = PipelineSpec(
        id="p", name="dup",
        sources=[Source(id="same", kind=SourceKind.UPLOAD, format=Format.CSV,
                        location="x")],
        targets=[Target(id="same", layer=Layer.BRONZE, dataset="d", table="t")],
    )
    assert any("duplicate id" in p for p in dup.validate())


def test_ir_catches_dq_rule_on_unknown_target():
    spec = _spec()
    spec.dq.append(DQRule(id="x", target="ghost", column="a", check="not_null"))
    assert any("targets unknown" in p for p in spec.validate())
