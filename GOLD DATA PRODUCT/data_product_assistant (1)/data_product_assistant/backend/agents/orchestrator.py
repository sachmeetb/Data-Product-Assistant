"""
orchestrator.py — Orchestrator Agent.

The single point of contact for users. Holds pipeline state, interprets
user messages, and drives the internal agent pipeline:
  Pipeline Generator → Test Agent (loop until pass) → Publisher Agent
"""

from .base import run_agent


async def run(
    user_input: str,
    pipeline_state: dict | None = None,
    conversation_history: list[dict] | None = None,
    session=None,
    context: dict | None = None,
) -> dict:
    """
    Invoke the orchestrator with the current user message and session context.

    Args:
        user_input:           Latest message from the user.
        pipeline_state:       Current pipeline execution state (or None if first turn).
        conversation_history: Ignored — MAF session tracks history automatically.
        session:              MAF AgentSession for this HTTP session.
        context:              Extra context dict merged into the prompt context block.
                              Used for relay mode (sub_agent_result, available_chips, etc.).
    """
    base_context: dict = {}
    if pipeline_state:
        base_context["pipeline_state"] = pipeline_state
    if context:
        base_context.update(context)
    return await run_agent(
        "orchestrator",
        user_input,
        context=base_context or None,
        session=session,
    )
