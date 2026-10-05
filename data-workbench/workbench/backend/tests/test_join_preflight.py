"""Bridge planner (join_preflight) — recognizes what the generator auto-bridges
and advises only on genuinely unbridgeable gaps. Uses a fake Neo4j session so
no live graph is needed.
"""

from workbench.backend import join_preflight as jp


class _Result(list):
    def single(self):
        return self[0] if self else None


class _FakeSession:
    """Dispatches session.run(query, **params) to canned rows by matching a
    distinctive fragment of each query the analyzer issues."""
    def __init__(self, base_tables, fk_edges, scd_policy_json=None, joins_json=None,
                 consumed=None):
        self._base = base_tables
        self._fk = fk_edges
        self._scd = scd_policy_json
        self._joins = joins_json
        self._consumed = consumed or []

    def run(self, query, **params):
        if "MAPS_SOURCE_COLUMN" in query:
            return _Result(self._base)
        if "HAS_DATASET_TRANSFORM" in query:
            return _Result([{"joins_json": self._joins, "scd_policy_json": self._scd}])
        if "CONSUMES" in query:
            return _Result(self._consumed)
        if "REFERENCES" in query:
            return _Result(self._fk)
        return _Result([])


def _row(uri, phys, product, cols, mapped=3):
    return {"uri": uri, "phys": phys, "product_uri": product, "product_name": product,
            "mapped_cols": mapped, "cols": cols}


def test_cross_product_scd2_recognized_as_auto_bridge():
    base = [
        _row("u:sh", "vw_salary_history", "prod02",
             ["employee_id", "amt", "effective_from", "effective_to", "is_current"], mapped=4),
        _row("u:jah", "vw_job_assignment_history", "prod01",
             ["employee_id", "job_id", "department_id", "effective_from", "effective_to", "is_current"], mapped=2),
    ]
    sess = _FakeSession(base, fk_edges=[], scd_policy_json='{"type":"scd2","effective_column":"effective_from"}')
    out = jp.analyze_dataset_connectivity(sess, "p", "ds")
    assert out["connected"] is True            # auto-bridgeable, NOT a blocking gap
    assert out["bridged_via"] == "auto_natural_key"
    assert out["cross_product"] is True
    # the recommended bridge carries the as-of interval for the SCD-2 grain
    preds = " ".join(j["predicate"] for j in out["recommended_joins"])
    assert "employee_id" in preds
    assert "effective_from <=" in preds and "effective_to IS NULL OR" in preds


def test_cross_product_current_state_recommends_equi():
    base = [
        _row("u:a", "vw_orders", "prodA", ["order_id", "customer_id", "amt"], mapped=4),
        _row("u:b", "vw_customer", "prodB", ["customer_id", "name"], mapped=1),
    ]
    sess = _FakeSession(base, fk_edges=[], scd_policy_json=None)
    out = jp.analyze_dataset_connectivity(sess, "p", "ds")
    assert out["connected"] is True and out["bridged_via"] == "auto_natural_key"
    preds = " ".join(j["predicate"] for j in out["recommended_joins"])
    assert "customer_id" in preds and "effective_from" not in preds  # plain equi


def test_no_shared_key_is_a_genuine_gap():
    base = [
        _row("u:a", "vw_a", "prodA", ["a_id", "x"], mapped=4),
        _row("u:b", "vw_b", "prodB", ["b_id", "y"], mapped=1),  # no shared key
    ]
    sess = _FakeSession(base, fk_edges=[], scd_policy_json=None)
    out = jp.analyze_dataset_connectivity(sess, "p", "ds")
    assert out["connected"] is False           # advise — don't guess
    assert out["unbridged_tables"] == ["vw_b"]


def test_single_component_fk_connected():
    base = [
        _row("u:a", "vw_a", "prodA", ["a_id", "b_id"]),
        _row("u:b", "vw_b", "prodA", ["b_id", "name"]),
    ]
    sess = _FakeSession(base, fk_edges=[{"from_uri": "u:a", "to_uri": "u:b"}])
    out = jp.analyze_dataset_connectivity(sess, "p", "ds")
    assert out["connected"] is True
    assert out.get("bridged_via") != "auto_natural_key"  # real FK, no bridge needed


# --- Gap A: transitive chaining over shared identity keys -------------------

_BANKING_ACCOUNT_COLS = ["account_id", "account_number", "product_code",
                         "currency", "ledger_balance", "status", "opened_date"]
_BANKING_CUSTOMER_COLS = ["customer_id", "name", "kyc_status", "onboarded_date"]
_BANKING_TXN_COLS = ["txn_id", "account_id", "customer_id", "amount", "channel"]


def test_two_hop_chain_through_mapped_ledger():
    """P1: account —account_id→ transaction —customer_id→ customer, all three
    mapped base tables, no FK. The chain must resolve without a human; the
    customer predicate references the TRANSACTION alias, not the anchor."""
    base = [
        _row("u:acc", "account", "prodA", _BANKING_ACCOUNT_COLS, mapped=9),
        _row("u:cus", "customer", "prodB", _BANKING_CUSTOMER_COLS, mapped=8),
        _row("u:txn", "transaction", "prodC", _BANKING_TXN_COLS, mapped=1),
    ]
    out = jp.analyze_dataset_connectivity(_FakeSession(base, fk_edges=[]), "p", "ds")
    assert out["connected"] is True
    assert out["bridged_via"] == "auto_natural_key"
    joins = out["recommended_joins"]
    aliases = [j["alias"] for j in joins]
    # dependency order: anchor, then transaction, then customer
    assert [j["dataset_uri"] for j in joins] == ["u:acc", "u:txn", "u:cus"]
    txn_alias = joins[1]["alias"]
    assert joins[1]["predicate"] == f"{txn_alias}.account_id = {aliases[0]}.account_id"
    assert joins[2]["predicate"] == f"{joins[2]['alias']}.customer_id = {txn_alias}.customer_id"
    assert joins[2]["via"] == {"table": "transaction", "key": "customer_id"}


def test_scd2_asof_decorates_chained_hop():
    """P5: an effective-dated table chained via an intermediate still gets the
    as-of interval pivoted on the ANCHOR's span start."""
    base = [
        _row("u:a", "vw_anchor", "prodA",
             ["a_id", "amt", "effective_from", "effective_to"], mapped=9),
        _row("u:j", "vw_junction", "prodB", ["a_id", "b_id"], mapped=2),
        _row("u:b", "vw_history", "prodC",
             ["b_id", "grade", "effective_from", "effective_to"], mapped=1),
    ]
    sess = _FakeSession(base, fk_edges=[], scd_policy_json='{"type":"scd2"}')
    out = jp.analyze_dataset_connectivity(sess, "p", "ds")
    assert out["connected"] is True
    hist = next(j for j in out["recommended_joins"] if j["dataset_uri"] == "u:b")
    anchor_alias = out["recommended_joins"][0]["alias"]
    assert f".effective_from <= {anchor_alias}.effective_from" in hist["predicate"]
    assert ".b_id = " in hist["predicate"]          # chained via the junction hop
    assert anchor_alias + ".a_id" not in hist["predicate"]


# --- Gap B: consumed-but-unmapped junction discovery -------------------------

def _consumed_txn(status="deployed", uri="u:txn", phys="transaction", kind="fact"):
    return {"uri": uri, "phys": phys, "relationship_kind": kind,
            "product_uri": "prodC", "product_name": "Transaction Master",
            "src_deployment_status": status, "cols": _BANKING_TXN_COLS}


def test_junction_discovered_from_consumed_products():
    """P2: transaction is NOT mapped — only consumed. It must be discovered,
    flagged bridge_only, and bridge account ↔ customer on different keys."""
    base = [
        _row("u:acc", "account", "prodA", _BANKING_ACCOUNT_COLS, mapped=9),
        _row("u:cus", "customer", "prodB", _BANKING_CUSTOMER_COLS, mapped=8),
    ]
    sess = _FakeSession(base, fk_edges=[], consumed=[_consumed_txn()])
    out = jp.analyze_dataset_connectivity(sess, "p", "ds")
    assert out["connected"] is True
    assert out["bridged_via"] == "auto_natural_key"
    joins = out["recommended_joins"]
    assert [j["dataset_uri"] for j in joins] == ["u:acc", "u:txn", "u:cus"]
    jx = joins[1]
    assert jx["bridge_only"] is True
    assert jx["via"]["role"] == "junction"
    assert set(jx["via"]["keys"]) == {"account_id", "customer_id"}
    assert "account_id" in jx["predicate"]
    assert "customer_id" in joins[2]["predicate"]
    assert out["junction_bridges"][0]["table"] == "transaction"
    assert "warnings" not in out                    # source is deployed


def test_junction_requires_different_keys():
    """P3: a consumed dataset sharing keys with only ONE side is not a
    junction — the gap stays a gap (advise, don't guess)."""
    base = [
        _row("u:acc", "account", "prodA", _BANKING_ACCOUNT_COLS, mapped=9),
        _row("u:cus", "customer", "prodB", _BANKING_CUSTOMER_COLS, mapped=8),
    ]
    one_sided = {"uri": "u:x", "phys": "account_flags", "relationship_kind": "fact",
                 "product_uri": "prodC", "product_name": "X",
                 "src_deployment_status": "deployed",
                 "cols": ["account_id", "flag"]}     # nothing linking to customer
    sess = _FakeSession(base, fk_edges=[], consumed=[one_sided])
    out = jp.analyze_dataset_connectivity(sess, "p", "ds")
    assert out["connected"] is False
    assert out["unbridged_tables"] == ["customer"]


def test_tied_junction_candidates_dont_guess():
    """P4: two equally-scored junction candidates → surfaced, never picked."""
    base = [
        _row("u:acc", "account", "prodA", _BANKING_ACCOUNT_COLS, mapped=9),
        _row("u:cus", "customer", "prodB", _BANKING_CUSTOMER_COLS, mapped=8),
    ]
    sess = _FakeSession(base, fk_edges=[], consumed=[
        _consumed_txn(uri="u:ledger1", phys="ledger_main"),
        _consumed_txn(uri="u:ledger2", phys="ledger_shadow"),
    ])
    out = jp.analyze_dataset_connectivity(sess, "p", "ds")
    assert out["connected"] is False
    tied = {c["table"] for c in out["junction_candidates"]}
    assert tied == {"ledger_main", "ledger_shadow"}
    assert "tie" in out["reason"] or "junction candidates" in out["reason"]


def test_junction_from_undeployed_source_warns():
    """P6: the junction's source product view isn't deployed → warning."""
    base = [
        _row("u:acc", "account", "prodA", _BANKING_ACCOUNT_COLS, mapped=9),
        _row("u:cus", "customer", "prodB", _BANKING_CUSTOMER_COLS, mapped=8),
    ]
    sess = _FakeSession(base, fk_edges=[], consumed=[_consumed_txn(status="pending")])
    out = jp.analyze_dataset_connectivity(sess, "p", "ds")
    assert out["connected"] is True
    assert any("not deployed" in w for w in out["warnings"])
