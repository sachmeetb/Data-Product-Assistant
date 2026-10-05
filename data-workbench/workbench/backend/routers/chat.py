import asyncio
import json
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from sqlmodel import Session, select

from ..chat_attachments import (
    AttachmentError,
    format_attachment_preamble,
    materialise_attachments,
)
from ..chat_runner import run_chat_turn
from ..config import BASE_PROJECT_DIR
from ..database import engine
from ..models import ChatMessage, ChatSession, Project

router = APIRouter()

HISTORY_TURN_LIMIT = 20


def _serialize_session(s: ChatSession) -> dict:
    return {
        "id": s.id,
        "project_id": s.project_id,
        "title": s.title,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


def _serialize_message(m: ChatMessage) -> dict:
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


@router.get("/api/projects/{project_id}/chat/sessions")
def list_sessions(project_id: int):
    with Session(engine) as session:
        project = session.get(Project, project_id)
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        rows = session.exec(
            select(ChatSession)
            .where(ChatSession.project_id == project_id)
            .order_by(ChatSession.updated_at.desc())
        ).all()
        return [_serialize_session(s) for s in rows]


@router.post("/api/projects/{project_id}/chat/sessions")
def create_session(project_id: int, body: dict | None = None):
    title = (body or {}).get("title", "") if isinstance(body, dict) else ""
    with Session(engine) as session:
        project = session.get(Project, project_id)
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        row = ChatSession(project_id=project_id, title=title or "")
        session.add(row)
        session.commit()
        session.refresh(row)
        return _serialize_session(row)


@router.get("/api/chat/sessions/{session_id}/messages")
def list_messages(session_id: int):
    with Session(engine) as session:
        sess = session.get(ChatSession, session_id)
        if not sess:
            raise HTTPException(status_code=404, detail="Session not found")
        rows = session.exec(
            select(ChatMessage)
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
        ).all()
        return [_serialize_message(m) for m in rows]


@router.delete("/api/chat/sessions/{session_id}")
def delete_session(session_id: int):
    with Session(engine) as session:
        sess = session.get(ChatSession, session_id)
        if not sess:
            raise HTTPException(status_code=404, detail="Session not found")
        msgs = session.exec(
            select(ChatMessage).where(ChatMessage.session_id == session_id)
        ).all()
        for m in msgs:
            session.delete(m)
        session.delete(sess)
        session.commit()
        return {"ok": True}


async def _safe_send(ws: WebSocket, msg: dict) -> bool:
    try:
        await ws.send_json(msg)
        return True
    except Exception:
        return False


def _load_history(session_id: int) -> list[dict]:
    with Session(engine) as session:
        rows = session.exec(
            select(ChatMessage)
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
        ).all()
    history: list[dict] = [{"role": r.role, "content": r.content} for r in rows]
    return history[-HISTORY_TURN_LIMIT * 2:]


def _persist_user_message(session_id: int, content: str) -> int:
    with Session(engine) as session:
        now = datetime.now(timezone.utc)
        row = ChatMessage(
            session_id=session_id, role="user", content=content, created_at=now
        )
        session.add(row)
        sess = session.get(ChatSession, session_id)
        if sess:
            sess.updated_at = now
            if not sess.title:
                sess.title = content[:60].strip()
            session.add(sess)
        session.commit()
        session.refresh(row)
        return row.id


def _persist_assistant_message(session_id: int, content: str,
                               tool_events: list[dict]) -> int:
    with Session(engine) as session:
        now = datetime.now(timezone.utc)
        row = ChatMessage(
            session_id=session_id,
            role="assistant",
            content=content,
            tool_events_json=json.dumps(tool_events),
            created_at=now,
        )
        session.add(row)
        sess = session.get(ChatSession, session_id)
        if sess:
            sess.updated_at = now
            session.add(sess)
        session.commit()
        session.refresh(row)
        return row.id


def _ensure_session(project_id: int, session_id: int | None) -> int:
    if session_id:
        with Session(engine) as session:
            sess = session.get(ChatSession, session_id)
            if sess and sess.project_id == project_id:
                return session_id
    with Session(engine) as session:
        row = ChatSession(project_id=project_id)
        session.add(row)
        session.commit()
        session.refresh(row)
        return row.id


@router.websocket("/ws/chat/{project_id}")
async def chat_websocket(websocket: WebSocket, project_id: int):
    from ..routers.websocket import ws_authenticate
    if ws_authenticate(websocket) is None:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_json()
            action = data.get("action")
            if action == "send_message":
                from .. import config as _config
                if _config.read_only():
                    await _safe_send(websocket, {"type": "error", "read_only": True,
                                                  "message": "Read-only instance — chat is disabled."})
                    continue
                text = (data.get("text") or "").strip()
                raw_session = data.get("session_id")
                session_id = int(raw_session) if raw_session else None
                attachments_raw = data.get("attachments") or []
                if not text and not attachments_raw:
                    await _safe_send(websocket, {"type": "error",
                                                  "message": "Empty message"})
                    continue
                await _handle_turn(websocket, project_id, text, session_id, attachments_raw)
            else:
                await _safe_send(websocket, {"type": "error",
                                              "message": f"Unknown action: {action}"})
    except WebSocketDisconnect:
        pass


async def _handle_turn(websocket: WebSocket, project_id: int,
                       text: str, session_id: int | None,
                       attachments_raw: list | None = None):
    with Session(engine) as session:
        project = session.get(Project, project_id)
        if not project:
            await _safe_send(websocket, {"type": "error",
                                          "message": "Project not found"})
            return
        project_code = project.project_code
        project_copy = Project(
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

    sid = _ensure_session(project_id, session_id)
    project_dir = str(BASE_PROJECT_DIR / project_code)

    # Validate + write attachments (small text/json/csv only) before any
    # other side effects so a bad attachment doesn't leave stale persisted
    # messages behind.
    try:
        materialised = materialise_attachments(
            attachments_raw or [], scope_dir=project_dir, session_token=f"engineer-{sid}"
        )
    except AttachmentError as ae:
        await _safe_send(websocket, {"type": "error", "message": f"Attachment: {ae}"})
        return

    history = _load_history(sid)
    user_visible = text or "(file attached)"
    _persist_user_message(sid, user_visible)

    await _safe_send(websocket, {
        "type": "turn_started",
        "session_id": sid,
        "project_code": project_code,
    })

    accumulated_text_parts: list[str] = []
    tool_events: list[dict] = []
    final_event: dict | None = None

    # Build prompt with optional attachments preamble.
    parts: list[str] = [text or "Please review the attached file(s)."]
    preamble = format_attachment_preamble(materialised)
    if preamble:
        parts.append("\n---\n" + preamble)
    prompt_for_model = "\n".join(parts)

    try:
        async for ev in run_chat_turn(prompt_for_model, project_copy, project_dir, history):
            t = ev.get("type")
            if t == "text_delta":
                accumulated_text_parts.append(ev.get("text", ""))
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

    content = "".join(accumulated_text_parts).strip()
    if content or tool_events:
        _persist_assistant_message(sid, content, tool_events)

    await _safe_send(websocket, {
        "type": "turn_complete",
        "session_id": sid,
        "is_error": bool(final_event.get("is_error")) if final_event.get("type") == "chat_complete" else (final_event.get("type") == "error"),
    })
