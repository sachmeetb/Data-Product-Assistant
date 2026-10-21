"""Session-scoped message queue for agent-to-user communication.

Each stage run gets a unique run_id.  When the agent needs user input it
POSTs a question to the backend; the backend parks the request here and
relays the question over WebSocket.  When the user responds, the response
is posted back here, unblocking the agent's HTTP call.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PendingQuestion:
    question_id: str
    run_id: str
    message_type: str  # notification | free_text | yes_no | multiple_choice
    prompt: str
    context: str | None = None
    options: list[dict[str, str]] | None = None
    default_value: str | None = None
    timeout_seconds: int = 300
    response_event: asyncio.Event = field(default_factory=asyncio.Event)
    response_value: str | None = None


class SessionMessageQueue:
    """In-memory, per-run message queue."""

    def __init__(self) -> None:
        # run_id -> list of pending questions
        self._pending: dict[str, dict[str, PendingQuestion]] = {}
        # run_id -> WebSocket send callback
        self._ws_callbacks: dict[str, Any] = {}

    def register_run(self, run_id: str, ws_send: Any) -> None:
        """Register a new run with its WebSocket send function."""
        self._pending[run_id] = {}
        self._ws_callbacks[run_id] = ws_send

    def cleanup_run(self, run_id: str) -> None:
        """Remove all state for a completed run."""
        self._pending.pop(run_id, None)
        self._ws_callbacks.pop(run_id, None)

    async def post_question(self, run_id: str, question_data: dict) -> str:
        """Agent posts a question.  Returns the question_id."""
        question_id = question_data.get("question_id") or str(uuid.uuid4())

        pq = PendingQuestion(
            question_id=question_id,
            run_id=run_id,
            message_type=question_data.get("message_type", "free_text"),
            prompt=question_data.get("prompt", ""),
            context=question_data.get("context"),
            options=question_data.get("options"),
            default_value=question_data.get("default_value"),
            timeout_seconds=question_data.get("timeout_seconds", 300),
        )

        if run_id not in self._pending:
            self._pending[run_id] = {}
        self._pending[run_id][question_id] = pq

        # Relay to frontend via WebSocket
        ws_send = self._ws_callbacks.get(run_id)
        if ws_send:
            await ws_send({
                "type": "agent_question",
                "question_id": question_id,
                "run_id": run_id,
                "message_type": pq.message_type,
                "prompt": pq.prompt,
                "context": pq.context,
                "options": pq.options,
                "default_value": pq.default_value,
                "timeout_seconds": pq.timeout_seconds,
            })

        return question_id

    async def wait_for_response(self, run_id: str, question_id: str) -> tuple[str | None, bool]:
        """Block until the user responds or timeout is reached.

        Returns (response_value, timed_out).
        """
        questions = self._pending.get(run_id, {})
        pq = questions.get(question_id)
        if not pq:
            return None, False

        try:
            await asyncio.wait_for(
                pq.response_event.wait(),
                timeout=pq.timeout_seconds,
            )
            return pq.response_value, False
        except asyncio.TimeoutError:
            # Notify frontend of timeout
            ws_send = self._ws_callbacks.get(run_id)
            if ws_send:
                await ws_send({
                    "type": "agent_timeout",
                    "question_id": question_id,
                    "default_used": pq.default_value is not None,
                })
            return pq.default_value, True
        finally:
            questions.pop(question_id, None)

    def list_pending(self, run_id: str) -> list[dict]:
        """Return the currently-unanswered parked questions for a run.

        Used by the MCP get_pending_questions tool so an engineer's Claude Code
        can poll for, and answer, mid-run agent questions (the headless
        equivalent of the WebSocket/UI answer path). Excludes questions whose
        response event is already set."""
        out: list[dict] = []
        for pq in self._pending.get(run_id, {}).values():
            if pq.response_event.is_set():
                continue
            out.append({
                "question_id": pq.question_id,
                "message_type": pq.message_type,
                "prompt": pq.prompt,
                "context": pq.context,
                "options": pq.options,
                "default_value": pq.default_value,
                "timeout_seconds": pq.timeout_seconds,
            })
        return out

    async def post_response(self, run_id: str, question_id: str, value: str) -> bool:
        """User posts a response.  Unblocks the waiting agent."""
        questions = self._pending.get(run_id, {})
        pq = questions.get(question_id)
        if not pq:
            return False

        pq.response_value = value
        pq.response_event.set()
        return True


# Singleton instance
message_queue = SessionMessageQueue()
