from typing import AsyncGenerator

from . import telemetry
from .config import PIPELINE_PLUGINS

ALLOWED_TOOLS = ["Read", "Write", "Edit", "Bash", "Glob", "Grep", "Skill"]

# Stages that operate on UNTRUSTED imported code (cmig reverse/forward) run under a
# stage-specific allowlist with NO Bash / execution-capable tools — imported code
# is data, never executed. Hashing detects mutation; this prevents execution.
CODE_MIGRATION_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "Skill"]

AGENT_QUESTION_INSTRUCTIONS = """

--- Agent-to-User Messaging ---
If you need input from the user during this task, use the agent_ask.py helper
script. The request blocks until the user responds in the UI.

Script location: {script_path}

Message types:
  notification     — informational, no response expected
  free_text        — open-ended text input
  yes_no           — binary yes/no choice
  multiple_choice  — single-select from options (radio buttons)
  checklist        — multi-select from options (checkboxes)

Basic usage (via Bash tool):
  python {script_path} --run-id {run_id} --type yes_no --prompt "Continue with all tables?"

With options (for multiple_choice or checklist):
  python {script_path} --run-id {run_id} --type checklist \\
    --prompt "Which schemas to discover?" \\
    --option "public:Public:Main application schema" \\
    --option "hr:HR:Human resources tables" \\
    --option "finance:Finance:Financial data"

Each --option is formatted as value:label or value:label:description.
For checklist, the printed response is comma-separated values (e.g. "public, hr").

Additional flags:
  --context "extra info"     Shown below the prompt
  --default "fallback"       Used if user doesn't respond in time
  --timeout 300              Seconds to wait (default: 300)

The script prints the user's response to stdout.

IMPORTANT: Only ask questions when genuinely needed or when the task instructions
explicitly tell you to ask the user for scope selection.
--- End Agent-to-User Messaging ---
"""


def _patch_sdk_parser():
    """Monkey-patch the SDK message parser to skip unknown message types."""
    try:
        import claude_agent_sdk._internal.message_parser as mp
        from claude_agent_sdk._errors import MessageParseError

        _original_parse = mp.parse_message

        def _tolerant_parse(data):
            try:
                return _original_parse(data)
            except MessageParseError as e:
                if "Unknown message type" in str(e):
                    return None
                raise

        mp.parse_message = _tolerant_parse

        try:
            import claude_agent_sdk._internal.client as client_mod
            client_mod.parse_message = _tolerant_parse
        except (ImportError, AttributeError):
            pass
    except (ImportError, AttributeError):
        pass


_patch_sdk_parser()


async def run_stage_streaming(
    prompt: str,
    project_dir: str,
    run_id: str | None = None,
    allowed_tools: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> AsyncGenerator[dict, None]:
    """Run a Claude Code SDK stage and yield streaming events as dicts.

    If run_id is provided, agent-to-user messaging instructions are appended
    to the prompt so the agent can ask the user questions mid-workflow.

    ``allowed_tools`` overrides the default tool allowlist for this run — used by
    cmig stages to run WITHOUT Bash (imported code is untrusted, never executed).

    ``env`` merges extra vars into the SDK subprocess environment (over
    os.environ). Tier-0 credential containment threads a registered source
    connection's DSN here as ``WB_SOURCE_DSN`` so the skill's bash invocation can
    expand it at run time without the credential ever entering the prompt text.
    """
    try:
        from claude_agent_sdk import query, ClaudeAgentOptions
        from claude_agent_sdk.types import (
            AssistantMessage,
            ResultMessage,
            TextBlock,
            ToolUseBlock,
            ThinkingBlock,
        )

        # Append agent-question instructions when a run_id is available
        full_prompt = prompt
        if run_id:
            from .config import BASE_DIR
            script_path = str(BASE_DIR / "scripts" / "agent_ask.py")
            full_prompt += AGENT_QUESTION_INSTRUCTIONS.format(run_id=run_id, script_path=script_path)

        system_prompt = (
            "You are executing a pipeline stage. Do not run git commands, explore the project "
            "directory structure, or verify Python package availability. The skill provides all "
            "instructions and scripts needed. Execute the task directly."
        )
        if run_id:
            system_prompt += (
                " You MUST complete ALL steps described in the prompt before finishing. "
                "After receiving a user response via agent_ask.py, you MUST immediately "
                "proceed to the next step — do NOT produce a final response until every "
                "step is done. If the prompt lists Steps 1 through N, you are not done "
                "until Step N is complete."
            )

        options = ClaudeAgentOptions(
            allowed_tools=allowed_tools or ALLOWED_TOOLS,
            permission_mode="acceptEdits",
            cwd=project_dir,
            max_turns=100,
            skills="all",
            plugins=PIPELINE_PLUGINS,
            env=env or {},
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": system_prompt,
            },
        )

        # Wrap the run in a span (no-op when telemetry is off). Keeping the span
        # current across the query() loop lets the SDK inject W3C trace context
        # into the spawned Claude Code CLI subprocess, so the CLI's spans parent
        # under this backend span — one end-to-end trace across the boundary.
        with telemetry.stage_span(
            "agent.stage_run",
            {"wb.run_id": run_id or "", "wb.project_dir": project_dir},
        ):
            async for message in query(prompt=full_prompt, options=options):
                if message is None:
                    continue

                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            yield {"type": "text_delta", "text": block.text}
                        elif isinstance(block, ToolUseBlock):
                            yield {
                                "type": "tool_use",
                                "tool": block.name,
                                "id": block.id,
                                "input": block.input if isinstance(block.input, dict) else str(block.input),
                            }
                        elif isinstance(block, ThinkingBlock):
                            yield {"type": "thinking", "text": block.thinking}

                elif isinstance(message, ResultMessage):
                    from .llm_usage import extract_usage
                    yield {
                        "type": "stage_complete",
                        "cost_usd": message.total_cost_usd,
                        "usage": extract_usage(message),
                        "duration_ms": message.duration_ms,
                        "session_id": message.session_id,
                        "is_error": message.is_error,
                    }

    except ImportError:
        yield {
            "type": "error",
            "message": "claude-agent-sdk is not installed. Install it with: pip install claude-agent-sdk",
        }
    except Exception as e:
        yield {"type": "error", "message": str(e)}
