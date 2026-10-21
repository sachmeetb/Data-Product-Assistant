from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .. import config
from ..auth import auth_enabled, decode_token, dev_user
from ..message_queue import message_queue
# Stage execution moved to ..stage_execution (shared with the MCP run_stage tool).
# _active_tasks / LOG_CAP_BYTES are re-exported here so existing
# `from .websocket import _active_tasks` callers (routers/stages.py,
# routers/projects.py) keep resolving the same objects.
from ..stage_execution import (  # noqa: F401
    LOG_CAP_BYTES,
    _active_tasks,
    start_stage_run,
)

router = APIRouter()


def ws_authenticate(websocket: WebSocket):
    """Resolve the WebSocket caller from the ``?token=`` query param.

    Returns the :class:`AuthUser` (or the dev default when auth is disabled), or
    ``None`` when auth is enabled and the token is missing/invalid — the caller
    must then close with code 1008 *before* accepting the socket.
    """
    if not auth_enabled():
        return dev_user()
    token = websocket.query_params.get("token", "")
    return decode_token(token) if token else None


@router.websocket("/ws/pipeline/{project_id}")
async def pipeline_websocket(websocket: WebSocket, project_id: int):
    user = ws_authenticate(websocket)
    if user is None:
        await websocket.close(code=1008)
        return
    await websocket.accept()

    try:
        while True:
            data = await websocket.receive_json()
            action = data.get("action")

            if action == "run_stage":
                if config.read_only():
                    await websocket.send_json({
                        "type": "error", "read_only": True,
                        "message": "Read-only instance — stages cannot be run.",
                    })
                    continue
                stage_number = data.get("stage_number")
                stage_config = data.get("stage_config", {})
                workflow_id = data.get("workflow_id")
                await _run_stage(websocket, project_id, stage_number, stage_config, workflow_id, user.role)

            elif action == "agent_response":
                run_id = data.get("run_id")
                question_id = data.get("question_id")
                value = data.get("value", "")
                if run_id and question_id:
                    await message_queue.post_response(run_id, question_id, value)

            else:
                await websocket.send_json({"type": "error", "message": f"Unknown action: {action}"})

    except WebSocketDisconnect:
        pass


async def _safe_ws_send(websocket: WebSocket, msg: dict) -> bool:
    """Send a message over WebSocket, returning False if disconnected."""
    try:
        await websocket.send_json(msg)
        return True
    except Exception:
        return False


async def _run_stage(websocket: WebSocket, project_id: int, stage_number: int,
                     stage_config: dict | None = None, workflow_id: str | None = None,
                     acting_role: str | None = None):
    """Thin WebSocket adapter over stage_execution.start_stage_run: streams the
    stage's events to this socket. All execution + persistence logic lives in
    start_stage_run, shared with the MCP run_stage tool."""
    async def sink(msg: dict):
        await _safe_ws_send(websocket, msg)

    await start_stage_run(
        project_id,
        stage_number,
        workflow_id=workflow_id,
        stage_config=stage_config,
        event_sink=sink,
        acting_role=acting_role if auth_enabled() else None,
    )
