"""Transform-intent interpreter — pure-Python (no Neo4j, no live SDK).

Locks the contract of `routers/transform_intent.py`: the deterministic heuristic
maps common phrasings to the correct structured DSL kind (NEVER raw prose), an
unknown kind coerces to the `expression` escape hatch, the core resolves input
NAMES to real source-column URIs (and warns on an unresolved one), and a raw
expression is portability-checked fail-open. Mirrors test_filter_intent.py.

Crucially this proves the AI path authors only the existing 16-kind DSL — it never
invents a shape the compiler can't emit.
"""

import asyncio

from workbench.backend.routers import transform_intent as ti


def _h(intent, target="", cols=None):
    return ti._heuristic_interpret(
        {"intent": intent, "target_column": target, "source_columns": cols or []}
    )


# ── heuristic: intent → structured kind (never prose) ────────────────────────


def test_heuristic_concat():
    r = _h("combine first and last name with a space", "full_name",
           [{"name": "first_name"}, {"name": "last_name"}])
    assert r["transform_kind"] == "concat"
    assert r["transform_inputs"] == ["first_name", "last_name"]
    assert r["transform_params"]["separator"] == " "
    assert r["transform_expression"] == ""  # structured, never raw prose


def test_heuristic_mask_keep_n():
    r = _h("mask all but the last 4 of ssn", cols=[{"name": "ssn"}])
    assert r["transform_kind"] == "mask"
    assert r["transform_params"] == {"algorithm": "keep_last", "keep_n": 4, "mask_char": "*"}


def test_heuristic_hash_algo():
    r = _h("hash the email with sha256", cols=[{"name": "email"}])
    assert r["transform_kind"] == "hash"
    assert r["transform_params"]["algorithm"] == "sha256"


def test_heuristic_format_case():
    r = _h("uppercase the country_code", cols=[{"name": "country_code"}])
    assert r["transform_kind"] == "format"
    assert r["transform_params"]["case"] == "upper"


def test_heuristic_date_difference():
    r = _h("age in years from date_of_birth", cols=[{"name": "date_of_birth"}])
    assert r["transform_kind"] == "date_difference"
    assert r["transform_params"]["unit"] == "year"


def test_heuristic_unknown_is_empty_not_prose():
    r = _h("some undefined proprietary scoring thing", cols=[{"name": "x"}])
    assert r["transform_kind"] == ""
    assert r["transform_expression"] == ""  # never echoes raw prose
    assert r["confidence"] <= 20


# ── kind normalization: only the 16-kind DSL, else the escape hatch ──────────


def test_normalize_kind_coerces_unknown_to_expression():
    assert ti._normalize_kind("frobnicate") == "expression"
    assert ti._normalize_kind("concat") == "concat"
    assert ti._normalize_kind("LOOKUP") == "lookup"
    assert ti._normalize_kind("") == ""


def test_valid_kinds_is_the_sixteen():
    assert len(ti.VALID_TRANSFORM_KINDS) == 16
    assert {"concat", "mask", "lookup", "date_difference", "expression"} <= ti.VALID_TRANSFORM_KINDS


# ── portability check is fail-open and never invents a shape ──────────────────


def test_portability_check_no_expression_is_noop():
    assert ti._portability_check("concat", "", "postgres") == []


def test_portability_check_never_raises():
    out = ti._portability_check("expression", "@@@ not sql @@@", "postgres")
    assert isinstance(out, list)  # fail-open: a list, never an exception


# ── core: resolve input NAMES → source-column URIs ───────────────────────────


def test_core_resolves_input_names_to_uris(monkeypatch):
    async def _fake_skill(inputs):
        return {
            "readback": "join", "transform_kind": "concat",
            "transform_inputs": ["first_name", "last_name"],
            "transform_params": {"separator": " "}, "transform_decorators": {},
            "transform_expression": "", "confidence": 90, "warnings": [],
            "grounded_columns": ["first_name", "last_name"],
        }, None

    monkeypatch.setattr(ti, "_run_interpreter_skill", _fake_skill)
    res = asyncio.run(ti.interpret_transform_intent(
        intent="combine names",
        source_columns=[
            {"name": "first_name", "uri": "column:p:public.c.first_name"},
            {"name": "last_name", "uri": "column:p:public.c.last_name"},
        ],
    ))
    assert res.transform_kind == "concat"
    assert res.transform_inputs == [
        "column:p:public.c.first_name", "column:p:public.c.last_name",
    ]


def test_core_warns_and_drops_unresolved_input(monkeypatch):
    async def _fake_skill(inputs):
        return {
            "readback": "x", "transform_kind": "concat",
            "transform_inputs": ["ghost_col"], "transform_params": {},
            "transform_decorators": {}, "transform_expression": "",
            "confidence": 90, "warnings": [], "grounded_columns": ["ghost_col"],
        }, None

    monkeypatch.setattr(ti, "_run_interpreter_skill", _fake_skill)
    res = asyncio.run(ti.interpret_transform_intent(
        intent="x", source_columns=[{"name": "real", "uri": "column:p:s.t.real"}],
    ))
    assert res.transform_inputs == []          # ghost didn't resolve to a URI
    assert res.confidence <= 60                # confidence downgraded
    assert any("resolve" in w.lower() for w in res.warnings)


def test_core_falls_back_to_heuristic_when_skill_absent(monkeypatch):
    async def _fail_skill(inputs):
        return {}, "claude-agent-sdk is not installed"

    monkeypatch.setattr(ti, "_run_interpreter_skill", _fail_skill)
    res = asyncio.run(ti.interpret_transform_intent(
        intent="mask all but the last 4 of ssn",
        source_columns=[{"name": "ssn", "uri": "column:p:s.t.ssn"}],
    ))
    assert res.transform_kind == "mask"
    assert res.transform_inputs == ["column:p:s.t.ssn"]
    assert res.transform_params.get("keep_n") == 4


def test_core_coerces_unknown_kind_to_expression(monkeypatch):
    async def _fake_skill(inputs):
        return {
            "readback": "x", "transform_kind": "frobnicate",
            "transform_inputs": [], "transform_params": {},
            "transform_decorators": {}, "transform_expression": "",
            "confidence": 50, "warnings": [], "grounded_columns": [],
        }, None

    monkeypatch.setattr(ti, "_run_interpreter_skill", _fake_skill)
    res = asyncio.run(ti.interpret_transform_intent(intent="x"))
    assert res.transform_kind == "expression"  # never leaks a non-DSL kind
