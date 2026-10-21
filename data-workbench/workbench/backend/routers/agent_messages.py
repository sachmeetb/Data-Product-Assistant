"""Blocking HTTP endpoint for agent-to-user questions.

The agent calls this via curl from inside a Claude Code SDK session.
The request blocks until the user responds via the WebSocket, then
returns the response as plain text so the agent can continue.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..message_queue import message_queue

router = APIRouter(prefix="/api/agent", tags=["agent-messages"])


class AskRequest(BaseModel):
    run_id: str
    message_type: str = "free_text"  # notification | free_text | yes_no | multiple_choice
    prompt: str
    context: str | None = None
    options: list[dict[str, str]] | None = None
    default_value: str | None = None
    timeout_seconds: int = 300


class AskResponse(BaseModel):
    question_id: str
    response: str | None
    timed_out: bool


@router.post("/ask", response_model=AskResponse)
async def ask_user(req: AskRequest):
    """Agent posts a question and blocks until the user responds.

    The agent calls this via:
        curl -s http://localhost:8000/api/agent/ask \\
            -X POST -H "Content-Type: application/json" \\
            -d '{"run_id":"...","prompt":"Which option?","message_type":"multiple_choice",...}'

    The response body contains the user's answer.
    """
    if req.run_id not in message_queue._pending and req.run_id not in message_queue._ws_callbacks:
        raise HTTPException(status_code=404, detail=f"No active run with id {req.run_id}")

    question_id = await message_queue.post_question(req.run_id, req.model_dump())
    response_value, timed_out = await message_queue.wait_for_response(req.run_id, question_id)

    return AskResponse(
        question_id=question_id,
        response=response_value,
        timed_out=timed_out,
    )
