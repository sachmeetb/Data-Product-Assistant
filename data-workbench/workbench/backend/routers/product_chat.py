"""Product Workbench chat — persisted, mirrors :mod:`routers.chat`.

Scoped by ``owner_email`` rather than ``project_id``: the wizard often
opens the chat before a project has been provisioned, so we can't key on
project. Sessions optionally carry a ``project_id`` once one exists, but
the dropdown the user sees is "all my chats" by email.

REST endpoints + WebSocket mirror ``routers.chat`` so frontend logic can
be near-identical between the two panels.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from sqlmodel import Session, select

from ..chat_attachments import (
    AttachmentError,
    format_attachment_preamble,
    materialise_attachments,
)
from ..config import BASE_DIR, BASE_PROJECT_DIR
from ..database import engine
from ..models import (
    ProductChatMessage,
    ProductChatSession,
    Project,
)
from ..product_chat_runner import run_product_chat_turn


router = APIRouter()

HISTORY_TURN_LIMIT = 20


# ── Serialisers ────────────────────────────────────────────────────────────


def _serialize_session(s: ProductChatSession) -> dict:
    return {
        "id": s.id,
        "owner_email": s.owner_email,
        "project_id": s.project_id,
        "title": s.title,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


def _serialize_message(m: ProductChatMessage) -> dict:
    try:
        tool_events = json.loads(m.tool_events_json or "[]")
    except json.JSONDecodeError:
        tool_events = []
    return {
        "id": m.id,
        "session_id": m.session_id,
        "role": m.role,
        "content": m.content,
        "tool_events": tool_events,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


# ── REST ──────────────────────────────────────────────────────────────────


@router.get("/api/chat/product/sessions")
def list_sessions(owner_email: str):
    if not owner_email:
        raise HTTPException(400, "owner_email is required")
    with Session(engine) as session:
        rows = session.exec(
            select(ProductChatSession)
            .where(ProductChatSession.owner_email == owner_email)
            .order_by(ProductChatSession.updated_at.desc())
        ).all()
        return [_serialize_session(s) for s in rows]


@router.post("/api/chat/product/sessions")
def create_session(body: dict | None = None):
    body = body or {}
    owner_email = (body.get("owner_email") or "").strip()
    if not owner_email:
        raise HTTPException(400, "owner_email is required")
    project_id = body.get("project_id")
    title = (body.get("title") or "").strip()
    with Session(engine) as session:
        row = ProductChatSession(
            owner_email=owner_email,
            project_id=project_id if isinstance(project_id, int) else None,
            title=title,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return _serialize_session(row)


@router.get("/api/chat/product/sessions/{session_id}/messages")
def list_messages(session_id: int):
    with Session(engine) as session:
        sess = session.get(ProductChatSession, session_id)
        if not sess:
            raise HTTPException(404, "Session not found")
        rows = session.exec(
            select(ProductChatMessage)
            .where(ProductChatMessage.session_id == session_id)
            .order_by(ProductChatMessage.created_at.asc(), ProductChatMessage.id.asc())
        ).all()
        return [_serialize_message(m) for m in rows]


@router.delete("/api/chat/product/sessions/{session_id}")
def delete_session(session_id: int):
    with Session(engine) as session:
        sess = session.get(ProductChatSession, session_id)
        if not sess:
            raise HTTPException(404, "Session not found")
        msgs = session.exec(
            select(ProductChatMessage).where(ProductChatMessage.session_id == session_id)
        ).all()
        for m in msgs:
            session.delete(m)
        session.delete(sess)
        session.commit()
        return {"ok": True}


# ── Helpers shared by the WebSocket handler ──────────────────────────────


async def _safe_send(ws: WebSocket, msg: dict) -> bool:
    try:
        await ws.send_json(msg)
        return True
    except Exception:
        return False


def _ensure_session(owner_email: str, session_id: Optional[int], project_id: Optional[int]) -> int:
    """Return a session id, creating one if the caller didn't supply a valid
    existing one for this owner."""
    if session_id:
        with Session(engine) as session:
            sess = session.get(ProductChatSession, session_id)
            if sess and sess.owner_email == owner_email:
                # Backfill project_id if it wasn't known when the session was
                # created (wizard provisions the project mid-flow).
                if project_id and not sess.project_id:
                    sess.project_id = project_id
                    session.add(sess)
                    session.commit()
                return session_id
    with Session(engine) as session:
        row = ProductChatSession(owner_email=owner_email, project_id=project_id)
        session.add(row)
        session.commit()
        session.refresh(row)
        return row.id


def _load_history(session_id: int) -> list[dict]:
    with Session(engine) as session:
        rows = session.exec(
            select(ProductChatMessage)
            .where(ProductChatMessage.session_id == session_id)
            .order_by(ProductChatMessage.created_at.asc(), ProductChatMessage.id.asc())
        ).all()
    history: list[dict] = [{"role": r.role, "content": r.content} for r in rows]
    return history[-HISTORY_TURN_LIMIT * 2:]


def _persist_user_message(session_id: int, content: str) -> int:
    with Session(engine) as session:
        now = datetime.now(timezone.utc)
        row = ProductChatMessage(
            session_id=session_id, role="user", content=content, created_at=now
        )
        session.add(row)
        sess = session.get(ProductChatSession, session_id)
        if sess:
            sess.updated_at = now
            if not sess.title:
                sess.title = content[:60].strip()
            session.add(sess)
        session.commit()
        session.refresh(row)
        return row.id


def _persist_assistant_message(session_id: int, content: str, tool_events: list[dict]) -> int:
    with Session(engine) as session:
        now = datetime.now(timezone.utc)
        row = ProductChatMessage(
            session_id=session_id,
            role="assistant",
            content=content,
            tool_events_json=json.dumps(tool_events),
            created_at=now,
        )
        session.add(row)
        sess = session.get(ProductChatSession, session_id)
        if sess:
            sess.updated_at = now
            session.add(sess)
        session.commit()
        session.refresh(row)
        return row.id


def _load_project(project_id: Optional[int]) -> Optional[Project]:
    if not project_id:
        return None
    with Session(engine) as session:
        project = session.get(Project, project_id)
        if not project:
            return None
        return Project(
            id=project.id,
            project_code=project.project_code,
            name=project.name,
            neo4j_host=project.neo4j_host,
            neo4j_port=project.neo4j_port,
            neo4j_user=project.neo4j_user,
            neo4j_password=project.neo4j_password,
            neo4j_database=project.neo4j_database,
            archetype=project.archetype,
            domain=project.domain,
        )


def _cwd_for_project(project: Optional[Project]) -> str:
    if project:
        return str(BASE_PROJECT_DIR / project.project_code)
    return str(BASE_DIR)


# ── WebSocket ─────────────────────────────────────────────────────────────


@router.websocket("/ws/product-chat/{owner_email}")
async def product_chat_websocket(websocket: WebSocket, owner_email: str):
    from ..routers.websocket import ws_authenticate
    from .. import config as _config
    if ws_authenticate(websocket) is None:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_json()
            action = data.get("action")
            if action in ("send_message", "delete_session") and _config.read_only():
                await _safe_send(websocket, {"type": "error", "read_only": True,
                                              "message": "Read-only instance — product chat is disabled."})
                continue
            if action == "send_message":
                text = (data.get("text") or "").strip()
                project_id = data.get("project_id")
                context = data.get("context") if isinstance(data.get("context"), dict) else None
                raw_session = data.get("session_id")
                session_id = int(raw_session) if raw_session else None
                attachments_raw = data.get("attachments") or []
                if not text and not attachments_raw:
                    await _safe_send(websocket, {"type": "error", "message": "Empty message"})
                    continue
                await _handle_turn(websocket, owner_email, text, session_id, project_id, context, attachments_raw)
            elif action == "delete_session":
                raw_session = data.get("session_id")
                if raw_session:
                    delete_session(int(raw_session))
                    await _safe_send(websocket, {"type": "session_deleted", "session_id": int(raw_session)})
            else:
                await _safe_send(websocket, {"type": "error", "message": f"Unknown action: {action}"})
    except WebSocketDisconnect:
        pass


async def _handle_turn(
    websocket: WebSocket,
    owner_email: str,
    text: str,
    session_id: Optional[int],
    project_id: Optional[int],
    context: Optional[dict],
    attachments_raw: Optional[list] = None,
):
    project = _load_project(project_id) if project_id else None
    cwd = _cwd_for_project(project)

    sid = _ensure_session(owner_email, session_id, project_id if isinstance(project_id, int) else None)

    # Materialise attachments first so we can fail fast on validation.
    try:
        materialised = materialise_attachments(
            attachments_raw or [], scope_dir=cwd, session_token=f"product-{sid}"
        )
    except AttachmentError as ae:
        await _safe_send(websocket, {"type": "error", "message": f"Attachment: {ae}"})
        return

    history = _load_history(sid)
    user_visible = text or "(file attached)"
    _persist_user_message(sid, user_visible)

    # Build the prompt the agent sees. Wizard ``context`` and the
    # attachment preamble both ride out-of-band so they don't pollute
    # the visible transcript.
    parts: list[str] = [text or "Please review the attached file(s)."]
    preamble = format_attachment_preamble(materialised)
    if preamble:
        parts.append("\n---\n" + preamble)
    if context:
        parts.append(
            "\n---\nWizard state (context, for your reference only — do not echo):\n"
            + f"```json\n{json.dumps(context, indent=2)}\n```"
        )
    prompt_for_model = "\n".join(parts)

    await _safe_send(websocket, {
        "type": "turn_started",
        "session_id": sid,
        "project_code": project.project_code if project else None,
    })

    accumulated_parts: list[str] = []
    tool_events: list[dict] = []
    final_event: Optional[dict] = None

    surface = (context or {}).get("surface") if isinstance(context, dict) else None
    try:
        async for ev in run_product_chat_turn(prompt_for_model, project, cwd, history, surface=surface):
            t = ev.get("type")
            if t == "text_delta":
                accumulated_parts.append(ev.get("text", ""))
            elif t == "tool_use":
                tool_events.append(ev)
            elif t in ("chat_complete", "error"):
                final_event = ev
            await _safe_send(websocket, ev)
    except Exception as e:
        final_event = {"type": "error", "message": str(e)}
        await _safe_send(websocket, final_event)

    if final_event is None:
        final_event = {"type": "chat_complete", "is_error": False}

    content = "".join(accumulated_parts).strip()
    if content or tool_events:
        _persist_assistant_message(sid, content, tool_events)

    await _safe_send(websocket, {
        "type": "turn_complete",
        "session_id": sid,
        "is_error": (
            bool(final_event.get("is_error"))
            if final_event.get("type") == "chat_complete"
            else (final_event.get("type") == "error")
        ),
    })
