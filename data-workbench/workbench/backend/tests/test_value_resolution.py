"""Unit tests for value_resolution.py — the ephemeral instance/value resolver.

Pure-logic: no live Postgres, Neo4j, or SDK. The fuzzy probe is exercised by
monkeypatching ``sql_executor.execute_select`` + ``neo4j_session``; the
orchestrator decisions by stubbing rank/probe. Embeddings are assumed
unavailable (lexical ranking path), matching a clean test venv.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace

import pytest

from workbench.backend import value_resolution as vr
from workbench.backend.sql_executor import SelectResult


# ── literal / pattern escaping (the #1 injection surface) ────────────────────


@pytest.mark.parametrize("raw,expected", [
    ("John Doe", "'John Doe'"),
    ("O'Brien", "'O''Brien'"),
    ("'; DROP TABLE users; --", "'''; DROP TABLE users; --'"),
    ("a''b", "'a''''b'"),
    ("back\\slash", "'back\\slash'"),  # backslash literal under standard strings
])
def test_pg_text_literal_escapes_quotes(raw, expected):
    assert vr._pg_text_literal(raw) == expected


def test_ilike_pattern_escapes_like_metacharacters():
    # % and _ must be escaped inside the LIKE pattern (using ! as the escape
    # char) so a user value can't act as a wildcard. ! itself is also escaped.
    # Backslash is NOT special in the ! scheme (unlike the old \\ scheme, which
    # broke on MySQL/Snowflake/Databricks where \ is a string-escape character).
    out = vr._ilike_pattern_literal("50%_x\\y")
    assert out == "'%50!%!_x\\y%'"
    # ! in the input is doubled so it doesn't act as an escape char.
    assert vr._ilike_pattern_literal("a!b") == "'%a!!b%'"
    # And single quotes still get SQL-escaped.
    assert vr._ilike_pattern_literal("o'b") == "'%o''b%'"


def test_fuzzy_prefix_tolerates_trailing_typos():
    assert vr._fuzzy_prefix("Sophie") == "Soph"   # so it prefilters "Sophia"
    assert vr._fuzzy_prefix("Miller") == "Mill"
    assert vr._fuzzy_prefix("Doe") == "Doe"        # short tokens stay whole
    assert vr._fuzzy_prefix("Li") == "Li"
    # rapidfuzz still scores the typo above the keep floor, below auto-resolve
    score = vr._fuzz_score("Sophie Miller", "Sophia Miller")
    assert vr.PROBE_MIN_FUZZY <= score < vr.AUTO_RESOLVE_FUZZY


def test_sanitize_strips_control_chars_and_caps_length():
    assert vr._sanitize_mention("a\x00b\x07c") == "abc"
    assert vr._sanitize_mention("  John   Doe  ") == "John Doe"
    long = "x" * 500
    assert len(vr._sanitize_mention(long)) == vr.MAX_MENTION_LEN


def test_tokenize_caps_and_drops_punctuation_only():
    assert vr._tokenize_mention("John  Doe") == ["John", "Doe"]
    assert vr._tokenize_mention("--- , .") == []
    assert len(vr._tokenize_mention(" ".join(str(i) for i in range(50)))) == vr.MAX_TOKENS


# ── pre-gate ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("msg", [
    "where does John Doe live?",
    'find the customer named "Acme Corp"',
    "who is Jane Smith",
    "orders for Germany",  # proper noun
])
def test_should_resolve_values_true(msg):
    assert vr.should_resolve_values(msg) is True


@pytest.mark.parametrize("msg", [
    "count orders by month",
    "how many customers are there",
    "show me the top 10 products by revenue",
    "total sales over time",
])
def test_should_resolve_values_false_for_analytic(msg):
    assert vr.should_resolve_values(msg) is False


# ── type compatibility ───────────────────────────────────────────────────────


def test_type_compatible_textual_mention():
    m = vr.ValueMention(text="John Doe", type_hint="person name")
    assert vr._type_compatible(m, "character varying") is True
    assert vr._type_compatible(m, "text") is True
    assert vr._type_compatible(m, "numeric") is False
    assert vr._type_compatible(m, "date") is False
    assert vr._type_compatible(m, "") is True  # unknown → don't over-filter


def test_type_compatible_numeric_mention():
    m = vr.ValueMention(text="1023", type_hint="id")
    assert vr._type_compatible(m, "bigint") is True
    assert vr._type_compatible(m, "varchar") is False


# ── attribute ranking (lexical path; embeddings unavailable) ─────────────────


def _employee_concepts():
    return [{
        "name": "Employee",
        "attributes": [
            {"name": "Employee Name", "definition": "full name of the employee",
             "represented_by_columns": [{"column_name": "employee_name", "column_uri": "u1"}]},
            {"name": "Salary", "definition": "annual pay",
             "represented_by_columns": [{"column_name": "salary", "column_uri": "u2"}]},
        ],
    }]


def _employee_views():
    return [{
        "view_schema": "public", "view_name": "vw_employee",
        "columns": [
            {"name": "employee_name", "data_type": "character varying"},
            {"name": "salary", "data_type": "numeric"},
            {"name": "department", "data_type": "varchar"},
        ],
    }]


def test_rank_attribute_candidates_picks_text_column(monkeypatch):
    monkeypatch.setattr(vr.embeddings, "available", lambda: False)
    m = vr.ValueMention(text="John Doe", type_hint="person name")
    cands = vr.rank_attribute_candidates(m, _employee_concepts(), _employee_views())
    assert cands, "expected at least one candidate"
    top = cands[0]
    assert top.column_name == "employee_name"
    assert top.view_name == "vw_employee"
    # numeric salary column is type-incompatible with a textual mention → dropped
    assert all(c.column_name != "salary" for c in cands)


# ── fuzzy probe ──────────────────────────────────────────────────────────────


@contextlib.contextmanager
def _fake_session(*_a, **_k):
    yield None


def _settings():
    return SimpleNamespace(neo4j_host="h", neo4j_port=7687, neo4j_user="u",
                           neo4j_password="p", neo4j_database="neo4j")


def _candidate():
    return vr.AttributeCandidate(
        attribute_name="Employee Name", column_name="employee_name", column_uri="u1",
        view_name="vw_employee", view_schema="public", data_type="varchar",
        score=0.9, view={"columns": [
            {"name": "employee_name", "data_type": "varchar"},
            {"name": "department", "data_type": "varchar"},
        ]},
    )


def _inputs():
    return SimpleNamespace(
        domain="hr", pg_connection="postgresql://x", product_uri="c1",
        allowed_pairs=[("public", "vw_employee")],
        concepts=_employee_concepts(), deployed_views=_employee_views(),
    )


def test_probe_rejects_non_allowlisted_pair(monkeypatch):
    monkeypatch.setattr(vr, "neo4j_session", _fake_session)
    called = {"n": 0}
    monkeypatch.setattr(vr.sql_executor, "execute_select",
                        lambda **k: called.__setitem__("n", called["n"] + 1) or SelectResult(status="ok"))
    cand = _candidate()
    bad_inputs = _inputs()
    bad_inputs.allowed_pairs = [("public", "vw_other")]
    assert vr.probe_column_for_value(SimpleNamespace(), bad_inputs, cand, "John Doe") == []
    assert called["n"] == 0  # never reached the DB


def test_probe_builds_escaped_ilike_and_ranks(monkeypatch):
    monkeypatch.setattr(vr, "neo4j_session", _fake_session)
    monkeypatch.setattr(vr, "_has_pg_trgm", lambda *a, **k: False)
    captured = {}

    def fake_exec(**kw):
        captured["sql"] = kw["sql"]
        captured["executed_by"] = kw["executed_by"]
        return SelectResult(
            status="ok",
            columns=[{"name": "_wb_match", "data_type": "varchar"},
                     {"name": "department", "data_type": "varchar"}],
            rows=[["John Doe", "Engineering"], ["Johnny Doe", "Sales"]],
            row_count=2,
        )

    monkeypatch.setattr(vr.sql_executor, "execute_select", fake_exec)
    out = vr.probe_column_for_value(_settings(), _inputs(), _candidate(), "John Doe")
    # token-AND ILIKE with escape, quoted identifiers, context column selected.
    # Patterns are typo-tolerant prefixes: "John"→"Joh", "Doe" (≤3) stays whole.
    assert 'ILIKE' in captured["sql"]
    assert "'%Joh%'" in captured["sql"] and "'%Doe%'" in captured["sql"]
    assert "ESCAPE '!'" in captured["sql"]
    assert '"public"."vw_employee"' in captured["sql"]
    assert captured["executed_by"] == "value-probe:hr"
    # exact match ranks first; label context attached
    assert out[0].value == "John Doe"
    assert out[0].fuzzy_score >= out[1].fuzzy_score
    assert out[0].pk_context.get("department") == "Engineering"


def test_probe_uses_pg_trgm_when_available(monkeypatch):
    monkeypatch.setattr(vr, "neo4j_session", _fake_session)
    monkeypatch.setattr(vr, "_has_pg_trgm", lambda *a, **k: True)
    captured = {}
    monkeypatch.setattr(vr.sql_executor, "execute_select",
                        lambda **kw: captured.__setitem__("sql", kw["sql"]) or
                        SelectResult(status="ok",
                                     columns=[{"name": "_wb_match", "data_type": "varchar"}],
                                     rows=[["John Doe"]], row_count=1))
    vr.probe_column_for_value(_settings(), _inputs(), _candidate(), "John Doe")
    assert "similarity(" in captured["sql"] and " % '" in captured["sql"]


# ── orchestrator decisions ───────────────────────────────────────────────────


def test_resolve_mentions_skipped_when_empty():
    out = vr.resolve_mentions(SimpleNamespace(), _inputs(), [])
    assert out.skipped_reason == "no value mentions"
    assert out.grounded_values == [] and out.disambiguation is None


def test_resolve_mentions_auto_resolves_unique_high_confidence(monkeypatch):
    monkeypatch.setattr(vr, "rank_attribute_candidates", lambda *a, **k: [_candidate()])
    monkeypatch.setattr(vr, "probe_column_for_value", lambda *a, **k: [
        vr.ValueCandidate(value="John Doe", column_name="employee_name",
                          view_name="vw_employee", view_schema="public", fuzzy_score=100.0),
    ])
    out = vr.resolve_mentions(SimpleNamespace(), _inputs(),
                              [{"text": "John Doe", "type_hint": "person name"}])
    assert out.disambiguation is None
    assert out.grounded_values == [{
        "column_name": "employee_name", "view_name": "vw_employee",
        "view_schema": "public", "value": "John Doe", "mention_text": "John Doe",
    }]
    assert out.trace[0]["status"] == "resolved"


def test_resolve_mentions_record_ambiguous(monkeypatch):
    monkeypatch.setattr(vr, "rank_attribute_candidates", lambda *a, **k: [_candidate()])
    monkeypatch.setattr(vr, "probe_column_for_value", lambda *a, **k: [
        vr.ValueCandidate(value="John Doe", column_name="employee_name",
                          view_name="vw_employee", view_schema="public", fuzzy_score=100.0),
        vr.ValueCandidate(value="John Doel", column_name="employee_name",
                          view_name="vw_employee", view_schema="public", fuzzy_score=96.0),
    ])
    out = vr.resolve_mentions(SimpleNamespace(), _inputs(),
                              [{"text": "John Doe", "type_hint": "person name"}])
    assert out.disambiguation is not None
    assert out.disambiguation["kind"] == "record"
    assert len(out.disambiguation["candidates"]) == 2
    assert out.grounded_values == []


def test_resolve_mentions_cross_column_becomes_concrete_candidates(monkeypatch):
    """When two columns both match, present CONCRETE record candidates (each
    tagged with its field) — never an abstract "which attribute" choice."""
    a_name = vr.AttributeCandidate(attribute_name="Employee Name", column_name="employee_name",
                                   column_uri="u1", view_name="vw_employee", view_schema="public",
                                   data_type="varchar", score=0.8)
    a_city = vr.AttributeCandidate(attribute_name="City", column_name="city",
                                   column_uri="u2", view_name="vw_location", view_schema="public",
                                   data_type="varchar", score=0.78)
    monkeypatch.setattr(vr, "rank_attribute_candidates", lambda *a, **k: [a_name, a_city])

    def fake_probe(settings, inputs, cand, text, **k):
        if cand.column_name == "employee_name":
            return [vr.ValueCandidate(value="Paris Hilton", column_name="employee_name",
                                      view_name="vw_employee", view_schema="public",
                                      fuzzy_score=90.0, field_label="Employee Name")]
        return [vr.ValueCandidate(value="Paris", column_name="city",
                                  view_name="vw_location", view_schema="public",
                                  fuzzy_score=92.0, field_label="City")]

    monkeypatch.setattr(vr, "probe_column_for_value", fake_probe)
    out = vr.resolve_mentions(SimpleNamespace(), _inputs(), [{"text": "Paris", "type_hint": ""}])
    dis = out.disambiguation
    assert dis["kind"] == "record"
    vals = {c["value"] for c in dis["candidates"]}
    assert vals == {"Paris Hilton", "Paris"}
    # labels carry the field when matches span columns
    assert any("City" in c["label"] for c in dis["candidates"])
    assert any("Employee Name" in c["label"] for c in dis["candidates"])


def test_resolve_mentions_data_overrides_bad_ranking(monkeypatch):
    """Regression: a person name semantically mis-ranked to a Gender column must
    still resolve to the name column, because Gender holds no matching value."""
    a_gender = vr.AttributeCandidate(attribute_name="Gender", column_name="gender",
                                     column_uri="u1", view_name="vw_employee", view_schema="public",
                                     data_type="varchar", score=0.62)
    a_name = vr.AttributeCandidate(attribute_name="Full Name", column_name="full_name",
                                   column_uri="u2", view_name="vw_employee", view_schema="public",
                                   data_type="varchar", score=0.55)
    # Gender ranked FIRST (the bug); the data must override.
    monkeypatch.setattr(vr, "rank_attribute_candidates", lambda *a, **k: [a_gender, a_name])

    def fake_probe(settings, inputs, cand, text, **k):
        if cand.column_name == "full_name":
            return [vr.ValueCandidate(value="Sophia Miller", column_name="full_name",
                                      view_name="vw_employee", view_schema="public", fuzzy_score=100.0)]
        return []  # gender column has nothing resembling "Sophia Miller"

    monkeypatch.setattr(vr, "probe_column_for_value", fake_probe)
    out = vr.resolve_mentions(SimpleNamespace(), _inputs(),
                              [{"text": "Sophia Miller", "type_hint": "person name"}])
    assert out.disambiguation is None
    assert out.grounded_values == [{
        "column_name": "full_name", "view_name": "vw_employee", "view_schema": "public",
        "value": "Sophia Miller", "mention_text": "Sophia Miller",
    }]


def test_resolve_mentions_not_found(monkeypatch):
    monkeypatch.setattr(vr, "rank_attribute_candidates", lambda *a, **k: [_candidate()])
    monkeypatch.setattr(vr, "probe_column_for_value", lambda *a, **k: [])
    out = vr.resolve_mentions(SimpleNamespace(), _inputs(),
                              [{"text": "Nobody Here", "type_hint": "person name"}])
    assert out.disambiguation["kind"] == "not_found"
    assert out.trace[0]["status"] == "not_found"
