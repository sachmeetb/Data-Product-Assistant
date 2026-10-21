"""HTTP/SSE entry points for CLI-driven stage execution.

The web UI dispatches stages over WebSocket (`routers/websocket.py`). The CLI
side of the workbench wants the same dispatch behavior but over plain HTTP
streaming so a slash command (`/run-stage`) can `curl -N` it.

This router mirrors `_run_stage` in `routers/websocket.py` — same DB writes,
same StageExecution audit trail, same `pipeline.build_prompt` + `run_stage_streaming`
invocation, same orphan-resilience — but the transport is Server-Sent Events.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from ..auth import auth_enabled, current_user
from ..authz import role_can_run_stage
from ..archetypes import STAGE_REGISTRY
from ..config import BASE_PROJECT_DIR
from ..database import engine
from ..message_queue import message_queue
from ..models import Project, StageExecution, StageRun, StageStatus, Workflow
from ..pipeline import build_prompt, get_stage
from ..sdk_runner import run_stage_streaming

router = APIRouter(tags=["cli"])

LOG_CAP_BYTES = 2_000_000


class StageRunRequest(BaseModel):
    workflow_id: str | None = None
    stage_config: dict | None = None


def _resolve_stage(
    session: Session,
    project: Project,
    stage_number: int,
    workflow_id: str | None,
) -> tuple[dict | None, str | None]:
    """Look up the stage definition + stage_id by stage_number/workflow_id.

    Mirrors the resolution logic in `routers/websocket.py:_run_stage` so the
    CLI sees exactly the same stage as the UI would.
    """
    stage_id: str | None = None
    stage_def: dict | None = None

    if workflow_id and project.multi_workflow:
        wf = session.exec(
            select(Workflow).where(
                Workflow.project_id == project.id,
                Workflow.workflow_id == workflow_id,
            )
        ).first()
        if wf and wf.workflow_json:
            stages = [s for s in json.loads(wf.workflow_json) if s.get("enabled", True)]
            if 1 <= stage_number <= len(stages):
                stage_id = stages[stage_number - 1]["stage_id"]
                stage_def = STAGE_REGISTRY.get(stage_id)
    elif project.workflow_json:
        workflow = [s for s in json.loads(project.workflow_json) if s.get("enabled", True)]
        if 1 <= stage_number <= len(workflow):
            stage_id = workflow[stage_number - 1]["stage_id"]
            stage_def = STAGE_REGISTRY.get(stage_id)

    if not stage_def:
        # `pipeline.get_stage` raises ValueError for unknown numbers — that's
        # a 404, not a 500. Treat it as "stage not in workflow."
        try:
            stage_def = get_stage(stage_number)
        except ValueError:
            stage_def = None
    return stage_def, stage_id


@router.post("/api/projects/{project_id}/stages/{stage_number}/stream")
async def run_stage_sse(
    project_id: int,
    stage_number: int,
    body: StageRunRequest | None = None,
    user=Depends(current_user),
):
    """Run a pipeline stage and stream events as SSE.

    The body is optional — `workflow_id` and `stage_config` default to None.
    Non-LLM stages are rejected: clients should POST `/stages/{n}/complete`
    instead (the UI does the same).
    """
    body = body or StageRunRequest()
    workflow_id = body.workflow_id
    stage_config = body.stage_config or {}

    # Resolve everything inside a short-lived session before we hand off to
    # the async generator. The session is closed before we start streaming
    # so it doesn't hold a connection open for the duration of the run.
    with Session(engine) as session:
        project = session.get(Project, project_id)
        if not project:
            raise HTTPException(404, "Project not found")

        stage_def, stage_id = _resolve_stage(session, project, stage_number, workflow_id)
        if not stage_def:
            raise HTTPException(404, f"Stage {stage_number} not found in project workflow")

        # Coarse role gate (PO↔Engineer). Mirrors the WS/MCP path.
        if auth_enabled() and not role_can_run_stage(user.role, stage_def.get("owner_role")):
            raise HTTPException(
                403,
                {"message": f"Your role '{user.role}' cannot run this stage "
                            f"(owned by '{stage_def.get('owner_role')}').",
                 "your_role": user.role},
            )

        # The web UI's WebSocket handler treats DQ test stages as
        # backend-driven subprocess runs (not SDK). For v1 the CLI path
        # restricts to LLM stages — DQ test execution is owned by the DQA
        # role anyway and goes through its own UI.
        if not stage_def.get("requires_llm"):
            raise HTTPException(
                400,
                {
                    "message": "Stage is non-LLM",
                    "stage_id": stage_id,
                    "hint": (
                        f"POST /api/projects/{project_id}/stages/{stage_number}/complete"
                        + (f"?workflow_id={workflow_id}" if workflow_id else "")
                    ),
                },
            )

        # Code-migration forward-engineering readiness gate — the same
        # code_migration_orchestrator.require_forward_ready the WS runner enforces,
        # so the CLI front door can't bypass the spec-approval invariant.
        if stage_id == "cmig_forward_engineer":
            from .. import code_migration_orchestrator as cmo
            ready = cmo.require_forward_ready(project, session)
            if not ready["ready"]:
                raise HTTPException(
                    409,
                    {"message": "forward-engineering is not ready", "missing": ready["missing"]},
                )

        # Find + flip the StageRun to running.
        q = select(StageRun).where(
            StageRun.project_id == project_id,
            StageRun.stage_number == stage_number,
        )
        if workflow_id:
            q = q.where(StageRun.workflow_id == workflow_id)
        stage_run = session.exec(q).first()
        if not stage_run:
            raise HTTPException(404, "StageRun row not found — re-create the workflow")
        stage_run.status = StageStatus.running
        stage_run.started_at = datetime.now(timezone.utc)
        session.add(stage_run)
        session.commit()

        prompt = build_prompt(stage_def, project, stage_config)
        project_dir = str(BASE_PROJECT_DIR / project.project_code)
        # Capture primitives we need after the session closes.
        stage_name = stage_def["name"]
        has_review = bool(stage_def.get("has_review"))
        cli_stage_id = stage_id
        project_code = project.project_code

    run_id = str(uuid.uuid4())

    # Question forwarding: agent_ask.py POSTs to /api/agent/ask, which calls
    # message_queue.post_question, which fans the question out through the
    # registered ws_send callback. We register a callback that puts the
    # question event into an asyncio.Queue, and the SSE generator drains
    # both that queue and the SDK event stream.
    question_queue: asyncio.Queue = asyncio.Queue()

    async def forward_question(msg: dict) -> None:
        # message_queue passes us full agent_question dicts (and agent_timeout
        # too). Push them onto our queue so the SSE generator emits them.
        await question_queue.put(msg)

    message_queue.register_run(run_id, forward_question)

    started_at = datetime.now(timezone.utc)
    stage_started_event = {
        "type": "stage_started",
        "stage_number": stage_number,
        "stage_name": stage_name,
        "run_id": run_id,
        "workflow_id": workflow_id,
        "started_at": started_at.isoformat(),
    }

    async def event_stream():
        events: list[dict] = [stage_started_event]
        tool_counts: dict[str, int] = {}
        total_bytes = len(json.dumps(stage_started_event))
        truncated = False
        final_event: dict | None = None

        def capture(ev: dict) -> None:
            nonlocal total_bytes, truncated
            if truncated:
                return
            try:
                encoded = json.dumps(ev)
            except Exception:
                encoded = json.dumps({"type": ev.get("type", "unknown")})
            size = len(encoded)
            if total_bytes + size > LOG_CAP_BYTES:
                truncated = True
                events.append({"type": "log_truncated", "at_event": len(events)})
                return
            events.append(ev)
            total_bytes += size
            if ev.get("type") == "tool_use":
                name = ev.get("tool") or "unknown"
                tool_counts[name] = tool_counts.get(name, 0) + 1

        def sse(ev: dict) -> str:
            return f"data: {json.dumps(ev)}\n\n"

        yield sse(stage_started_event)

        # Merge the SDK event stream with the agent_question queue: whichever
        # has the next item ready, yield that. The SDK stream is the
        # authoritative completion signal; once it ends, drain any pending
        # questions and emit the final event.
        from ..sdk_runner import CODE_MIGRATION_TOOLS
        _cli_tools = CODE_MIGRATION_TOOLS if cli_stage_id in (
            "cmig_reverse_engineer", "cmig_forward_engineer") else None
        sdk_iter = run_stage_streaming(prompt, project_dir, run_id, allowed_tools=_cli_tools)
        sdk_task: asyncio.Task | None = None
        q_task: asyncio.Task | None = None

        try:
            sdk_task = asyncio.create_task(sdk_iter.__anext__())
            q_task = asyncio.create_task(question_queue.get())

            while True:
                done, _ = await asyncio.wait(
                    [t for t in (sdk_task, q_task) if t is not None],
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if q_task in done:
                    try:
                        q_event = q_task.result()
                        # Question events go straight through — they don't
                        # count toward the capped transcript (engineer needs
                        # to see them) but we capture them for audit.
                        capture(q_event)
                        yield sse(q_event)
                    except Exception:
                        pass
                    q_task = asyncio.create_task(question_queue.get())

                if sdk_task in done:
                    try:
                        event = sdk_task.result()
                    except StopAsyncIteration:
                        sdk_task = None
                        break
                    except Exception as e:
                        final_event = {"type": "error", "message": str(e)}
                        sdk_task = None
                        break

                    if event.get("type") in ("stage_complete", "error"):
                        final_event = event
                        sdk_task = None
                        break
                    capture(event)
                    yield sse(event)
                    sdk_task = asyncio.create_task(sdk_iter.__anext__())
        finally:
            # Cancel any pending tasks so we don't leak fds on disconnect.
            for t in (sdk_task, q_task):
                if t is not None and not t.done():
                    t.cancel()
            message_queue.cleanup_run(run_id)

        if final_event is None:
            final_event = {
                "type": "stage_complete",
                "is_error": False,
                "cost_usd": None,
                "session_id": None,
            }
        capture(final_event)

        completed_at = datetime.now(timezone.utc)
        new_status: str | None = None

        # Mirror the websocket handler's DB writes so UI / CLI converge on
        # exactly the same StageRun + StageExecution state regardless of who
        # ran the stage.
        with Session(engine) as session:
            q = select(StageRun).where(
                StageRun.project_id == project_id,
                StageRun.stage_number == stage_number,
            )
            if workflow_id:
                q = q.where(StageRun.workflow_id == workflow_id)
            sr = session.exec(q).first()
            if sr and final_event:
                if final_event.get("type") == "stage_complete" and not final_event.get("is_error"):
                    if has_review:
                        sr.status = StageStatus.awaiting_review
                        new_status = "awaiting_review"
                    else:
                        sr.status = StageStatus.complete
                        new_status = "complete"
                    sr.cost_usd = final_event.get("cost_usd")
                    sr.session_id = final_event.get("session_id")
                else:
                    sr.status = StageStatus.failed
                    sr.error_message = final_event.get("message", "Unknown error")
                    new_status = "failed"

                # cmig fail-closed post-run validation (parity with the WS runner).
                if new_status in ("complete", "awaiting_review") and cli_stage_id in (
                    "cmig_reverse_engineer", "cmig_forward_engineer"):
                    try:
                        from .. import code_migration_orchestrator as cmo
                        from ..stage_execution import _validate_cmig_conversion
                        proj2 = session.get(Project, project_id)
                        if cli_stage_id == "cmig_reverse_engineer":
                            if not cmo.ingest_reverse_engineered_spec(session, proj2):
                                sr.status = StageStatus.failed
                                sr.error_message = "Reverse-engineering produced no valid codespec.json."
                                new_status = "failed"
                        else:
                            ok, has_actions = _validate_cmig_conversion(project_code)
                            cmo.record_conversion(session, proj2, ok=ok, has_actions=has_actions)
                            if not ok:
                                sr.status = StageStatus.failed
                                sr.error_message = "Forward-engineering produced no valid conversion output."
                                new_status = "failed"
                    except Exception:
                        pass
                sr.completed_at = completed_at
                session.add(sr)
                session.commit()
                session.refresh(sr)

                session.add(StageExecution(
                    stage_run_id=sr.id,
                    project_id=project_id,
                    workflow_id=workflow_id,
                    stage_number=stage_number,
                    run_id=run_id,
                    started_at=started_at,
                    completed_at=completed_at,
                    status=new_status or "failed",
                    cost_usd=final_event.get("cost_usd"),
                    session_id=final_event.get("session_id"),
                    error_message=(final_event.get("message") if final_event.get("type") == "error" else None),
                    event_count=len(events),
                    tool_counts_json=json.dumps(tool_counts),
                    log_json=json.dumps(events),
                    truncated=truncated,
                ))
                session.commit()
                try:
                    from .. import llm_usage
                    _proj = session.get(Project, project_id)
                    llm_usage.record_usage(
                        source="stage", usage=final_event.get("usage"),
                        project_code=_proj.project_code if _proj else None,
                        run_id=run_id, session=session,
                    )
                except Exception:
                    pass

        yield sse(final_event)
        if new_status:
            yield sse({
                "type": "stage_status_changed",
                "stage_number": stage_number,
                "workflow_id": workflow_id,
                "status": new_status,
            })

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


class AgentResponseRequest(BaseModel):
    run_id: str
    question_id: str
    value: str


@router.post("/api/agent/respond")
async def agent_respond(req: AgentResponseRequest):
    """Deliver a user response to a parked agent question.

    Sibling to `routers/agent_messages.py:/api/agent/ask`. The UI delivers
    responses over its WebSocket `agent_response` action; the CLI uses this
    HTTP entry point so an engineer can answer an `agent_ask.py` prompt
    without opening the browser.
    """
    ok = await message_queue.post_response(req.run_id, req.question_id, req.value)
    if not ok:
        raise HTTPException(404, "No pending question for that run_id/question_id")
    return {"status": "ok"}
