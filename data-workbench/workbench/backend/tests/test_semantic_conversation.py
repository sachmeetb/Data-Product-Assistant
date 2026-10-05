"""Conversational Semantic Q&A tests — the orchestrator "above" the retrieval
modes (POST /api/marketplace/conversation + session CRUD).

Exercises the SQLite/session side without a live Neo4j or LLM: the
ontology-grounded router pass (``marketplace_chat.route_turn``) and the NL→SQL
query pass (``marketplace_chat.answer_question``) are monkeypatched with canned
results, so these assert the wiring — session creation, message persistence,
rolling session_context merge, routing-trace prepend, and the action-specific
response shape.
"""

from __future__ import annotations

import asyncio

from sqlmodel import Session, select

from workbench.backend import marketplace_chat as mc
from workbench.backend.database import engine
from workbench.backend.models import SemanticChatMessage, SemanticChatSession
from workbench.backend.routers import marketplace as mp


def _conversation(**kwargs):
    body = mp._ConversationBody(**kwargs)
    with Session(engine) as s:
        return asyncio.run(mp.marketplace_conversation(body, s))


# ── session CRUD ─────────────────────────────────────────────────────────────


def test_session_crud(session):
    created = mp.create_semantic_session({"domain": "products_sales"}, session)
    sid = created["id"]
    assert created["domain"] == "products_sales"
    assert created["session_context"] == {}

    listed = mp.list_semantic_sessions(domain="products_sales", session=session)
    assert any(s["id"] == sid for s in listed)

    msgs = mp.list_semantic_messages(sid, session)
    assert msgs["session"]["id"] == sid
    assert msgs["messages"] == []

    assert mp.delete_semantic_session(sid, session) == {"ok": True}
    assert session.get(SemanticChatSession, sid) is None


# ── clarify turn: no query, persists transcript + merges memory ──────────────


def test_clarify_turn_persists_and_merges_context(monkeypatch):
    async def fake_route(settings, domain, user_message, conversation, ctx):
        return (
            mc.RouterDecision(
                action="clarify",
                reply="Which customer do you mean?",
                clarification_options=["By ID", "By segment"],
                grounding={"matched_concepts": ["Customer"], "notes": "ambiguous"},
                session_context_update={"focus_entities": ["Customer"]},
            ),
            [{"name": "Customer"}],
        )

    monkeypatch.setattr(mc, "route_turn", fake_route)

    resp = _conversation(domain="products_sales", user_message="what is the name")
    assert resp["action"] == "clarify"
    assert resp["reply"] == "Which customer do you mean?"
    assert resp["clarification_options"] == ["By ID", "By segment"]
    assert resp["payload"] is None
    # Routing-only trace, marked conversational.
    assert resp["trace"]["retrieval_mode"] == "conversational"
    assert [s["key"] for s in resp["trace"]["steps"]] == ["routing"]
    # Memory merged + persisted.
    assert resp["session_context"]["focus_entities"] == ["Customer"]
    sid = resp["session_id"]
    with Session(engine) as s:
        sess = s.get(SemanticChatSession, sid)
        assert '"focus_entities"' in sess.session_context_json
        rows = s.exec(
            select(SemanticChatMessage).where(SemanticChatMessage.session_id == sid)
            .order_by(SemanticChatMessage.id.asc())
        ).all()
        assert [r.role for r in rows] == ["user", "assistant"]
        assert rows[0].content == "what is the name"


# ── query turn: invokes the pipeline + prepends the routing trace step ───────


def test_query_turn_prepends_routing_step(monkeypatch):
    async def fake_route(settings, domain, user_message, conversation, ctx):
        return (
            mc.RouterDecision(
                action="query",
                resolved_question="The top 5 customers by revenue.",
                grounding={"matched_concepts": ["Customer"], "notes": "complete"},
                session_context_update={"last_query": "top 5 customers by revenue"},
            ),
            [{"name": "Customer"}],
        )

    captured = {}

    async def fake_answer(settings, domain, question, **kwargs):
        captured["question"] = question
        captured["retrieval_mode"] = kwargs.get("retrieval_mode")
        return {
            "status": "ok",
            "message": "Here are the top customers.",
            "synthesized_answer": "Acme leads with $1.2M.",
            "sql": "SELECT 1",
            "rows": [[1]],
            "columns": [{"name": "x", "data_type": "int"}],
            "token_usage": {"total_tokens": 100, "input_tokens": 60, "output_tokens": 40},
            "trace": {"retrieval_mode": "concept_guided",
                      "steps": [{"key": "generate_sql", "label": "Generated SQL", "detail": {}}]},
        }

    monkeypatch.setattr(mc, "route_turn", fake_route)
    monkeypatch.setattr(mc, "answer_question", fake_answer)

    resp = _conversation(domain="products_sales", user_message="top customers",
                         retrieval_submode="concept_guided")
    assert resp["action"] == "query"
    # The router's resolved_question is what reaches the pipeline.
    assert captured["question"] == "The top 5 customers by revenue."
    assert captured["retrieval_mode"] == "concept_guided"
    assert resp["payload"]["status"] == "ok"
    assert resp["reply"] == "Acme leads with $1.2M."
    # Routing step prepended ahead of the query trace.
    keys = [s["key"] for s in resp["trace"]["steps"]]
    assert keys[0] == "routing"
    assert "generate_sql" in keys
    assert resp["trace"]["steps"][0]["detail"]["resolved_question"] == "The top 5 customers by revenue."
    # Memory merged.
    assert resp["session_context"]["last_query"] == "top 5 customers by revenue"


# ── memory survives across turns within one session ──────────────────────────


def test_disambiguation_turn_then_pick_reconciles(monkeypatch):
    """A query turn whose record can't be uniquely resolved becomes a `clarify`
    turn with candidate chips; the next turn's pick reconciles against the stored
    pending_disambiguation and re-runs the query WITHOUT routing."""
    route_calls = {"n": 0}

    async def fake_route(settings, domain, user_message, conversation, ctx):
        route_calls["n"] += 1
        return (
            mc.RouterDecision(action="query",
                              resolved_question="Where does John Doe live?",
                              grounding={"matched_concepts": [], "notes": ""}),
            [{"name": "Employee"}],
        )

    answer_calls = {"n": 0, "resolved_values": None}

    async def fake_answer(settings, domain, question, **kwargs):
        answer_calls["n"] += 1
        answer_calls["resolved_values"] = kwargs.get("resolved_values")
        if not kwargs.get("resolved_values"):
            return {
                "status": "needs_disambiguation",
                "disambiguation": {
                    "kind": "record", "mention_text": "John Doe",
                    "prompt": "Which John Doe did you mean?",
                    "candidates": [
                        {"label": "John Doe — Engineering, NYC", "value": "John Doe",
                         "column_name": "employee_name", "view_name": "vw_employee",
                         "view_schema": "public"},
                        {"label": "John Doel — Sales, LA", "value": "John Doel",
                         "column_name": "employee_name", "view_name": "vw_employee",
                         "view_schema": "public"},
                    ],
                    "attribute_options": [], "already_grounded": [],
                },
                "trace": {"retrieval_mode": "concept_guided", "steps": []},
                "token_usage": {"total_tokens": 50},
            }
        return {
            "status": "ok", "message": "John Doe lives in NYC.",
            "synthesized_answer": "John Doe lives in New York City.",
            "sql": "SELECT 1", "rows": [["NYC"]],
            "columns": [{"name": "city", "data_type": "text"}],
            "token_usage": {"total_tokens": 80},
            "trace": {"retrieval_mode": "concept_guided",
                      "steps": [{"key": "generate_sql", "label": "x", "detail": {}}]},
        }

    monkeypatch.setattr(mc, "route_turn", fake_route)
    monkeypatch.setattr(mc, "answer_question", fake_answer)

    # Turn 1 — query that can't uniquely resolve → clarify with chips.
    first = _conversation(domain="hr", user_message="where does John Doe live?")
    sid = first["session_id"]
    assert first["action"] == "clarify"
    assert first["clarification_options"] == ["John Doe — Engineering, NYC", "John Doel — Sales, LA"]
    assert first["payload"] is None
    assert first["session_context"]["pending_disambiguation"]["mention_text"] == "John Doe"

    # Turn 2 — the user clicks a chip; reconciliation runs the query, skips router.
    second = _conversation(session_id=sid, domain="hr",
                           user_message="John Doe — Engineering, NYC")
    assert second["action"] == "query"
    assert second["payload"]["status"] == "ok"
    assert answer_calls["resolved_values"] == [{
        "mention_text": "John Doe", "value": "John Doe",
        "column_name": "employee_name", "view_name": "vw_employee", "view_schema": "public",
    }]
    assert route_calls["n"] == 1  # router NOT called on the pick turn
    # Pending state cleared after the pick.
    assert "pending_disambiguation" not in second["session_context"]


def test_pending_pick_affirmative_and_ordinal():
    """A single-candidate confirm ('yes that's her') and an ordinal ('the first
    one') both reconcile to the stored candidate."""
    one = {"kind": "record", "mention_text": "Sophie", "resolved_question": "What is Sophie's email?",
           "already_grounded": [], "candidates": [
               {"label": "Sophia — sophia.miller149@example.com", "value": "Sophia",
                "column_name": "first_name", "view_name": "vw_employee", "view_schema": "public"}]}
    r = mp._match_pending_pick("yes that's her", one)
    assert r is not None
    assert r["resolved_values"][0]["value"] == "Sophia"
    assert r["resolved_question"] == "What is Sophie's email?"

    multi = {"kind": "record", "mention_text": "Paris", "resolved_question": "Q",
             "already_grounded": [], "candidates": [
                 {"label": "Paris Hilton", "value": "Paris Hilton", "column_name": "full_name",
                  "view_name": "v", "view_schema": "public"},
                 {"label": "Paris (City)", "value": "Paris", "column_name": "city",
                  "view_name": "v2", "view_schema": "public"}]}
    assert mp._match_pending_pick("the first one", multi)["resolved_values"][0]["value"] == "Paris Hilton"
    assert mp._match_pending_pick("#2", multi)["resolved_values"][0]["value"] == "Paris"
    # An affirmative with MULTIPLE candidates can't be auto-picked.
    assert mp._match_pending_pick("yes", multi) is None
    # A new unrelated question doesn't false-match.
    assert mp._match_pending_pick("what is the headcount", one) is None


def test_memory_persists_across_turns(monkeypatch):
    async def fake_route(settings, domain, user_message, conversation, ctx):
        # Echo whatever context was passed in so we can assert it accumulates.
        seen = list((ctx or {}).get("focus_entities", []))
        return (
            mc.RouterDecision(
                action="chat", reply=f"seen={seen}",
                session_context_update={"focus_entities": seen + [user_message]},
            ),
            [],
        )

    monkeypatch.setattr(mc, "route_turn", fake_route)

    first = _conversation(domain="d", user_message="A")
    sid = first["session_id"]
    assert first["session_context"]["focus_entities"] == ["A"]

    second = _conversation(session_id=sid, domain="d", user_message="B")
    assert second["session_id"] == sid
    # Second turn saw the first turn's memory.
    assert second["reply"] == "seen=['A']"
    assert second["session_context"]["focus_entities"] == ["A", "B"]
