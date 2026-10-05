from typing import AsyncGenerator

from .config import SKILLS_DIR, PIPELINE_PLUGINS
from .models import Project

CHAT_ALLOWED_TOOLS = ["Read", "Bash", "Grep", "Glob", "Skill"]

SKILL_NAME = "project-chat-assistant"


def _build_system_prompt(project: Project) -> str:
    archetype = project.archetype or "dd"
    domain = project.domain or "(no domain set)"
    code = project.project_code
    host = project.neo4j_host
    port = project.neo4j_port
    user = project.neo4j_user
    password = project.neo4j_password
    database = project.neo4j_database
    return (
        f"You are the Data Workbench assistant for project `{code}` "
        f"(archetype `{archetype}`, domain `{domain}`). Your job is to answer "
        f"questions about this project's Neo4j knowledge graph, explain results, "
        f"and help the user understand the workbench.\n\n"
        "## Neo4j connection (use these credentials — do not look for them)\n"
        f"- host: {host}\n"
        f"- port: {port}\n"
        f"- user: {user}\n"
        f"- password: {password}\n"
        f"- database: {database}\n\n"
        "## How to run a query\n"
        f"Every data question starts with exactly this shape via the Bash tool "
        "(substitute your Cypher):\n\n"
        "```\n"
        f"python {SKILLS_DIR}/{SKILL_NAME}/scripts/run_cypher.py \\\n"
        f"  --project-code '{code}' \\\n"
        f"  --host '{host}' --port {port} \\\n"
        f"  --user '{user}' --password '{password}' \\\n"
        f"  --database '{database}' \\\n"
        '  --query "MATCH (:Project {projectCode: \'' + code + '\'})-[:HAS_CATALOG]->(c:Catalog) RETURN c.name"\n'
        "```\n\n"
        "## Hard rules\n"
        f"- Load the `{SKILL_NAME}` skill first (once per turn) for the ontology "
        "reference and query templates.\n"
        f"- Every Cypher query MUST scope to projectCode='{code}'. Never query "
        "other projects. If a query could match other projects, refuse.\n"
        "- Run `run_cypher.py` for any factual claim about the user's data. "
        "Never answer data questions from prior knowledge.\n"
        "- Before recommending action, surface at least one piece of graph "
        "evidence (counts, batch ids, measurements) that supports it.\n"
        "- READ-ONLY. Never write to Neo4j, never mutate graph state, never "
        "approve/reject reviews, never trigger stages. If asked, explain how "
        "the user can do it in the UI.\n\n"
        "## Forbidden — do not do any of these\n"
        "- Do NOT read `workbench.db` or any SQLite file to find credentials "
        "or configuration. All credentials you need are in this prompt.\n"
        "- Do NOT grep the codebase for `NEO4J_PASSWORD` or similar. The "
        "password is above.\n"
        "- Do NOT brute-force or guess passwords in any loop. If an auth "
        "error occurs, state the error and stop.\n"
        "- Do NOT run `run_cypher.py --help`, list files, inspect `.env`, or "
        "explore the project directory. The invocation above is complete.\n"
        "- Do NOT echo the password in your response to the user.\n\n"
        "Response style: terse, evidence-led, propose a concrete next action "
        "when relevant, offer one follow-up question the user might ask next."
    )


def _build_turn_prompt(user_message: str, history: list[dict]) -> str:
    parts = [f"FIRST: Load the {SKILL_NAME} skill using the Skill tool."]
    if history:
        parts.append("\nPrior conversation (for context):")
        for m in history:
            role = m.get("role", "user")
            content = (m.get("content") or "").strip()
            if not content:
                continue
            parts.append(f"\n[{role}]\n{content}")
        parts.append("")
    parts.append(f"\nCurrent user message:\n{user_message}")
    return "\n".join(parts)


async def run_chat_turn(
    user_message: str,
    project: Project,
    project_dir: str,
    history: list[dict] | None = None,
) -> AsyncGenerator[dict, None]:
    """Run one chat turn through the Claude Code SDK, streaming events.

    history is a list of {role, content} dicts from prior turns in the session.
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

        options = ClaudeAgentOptions(
            allowed_tools=CHAT_ALLOWED_TOOLS,
            permission_mode="acceptEdits",
            cwd=project_dir,
            max_turns=15,
            skills="all",
            plugins=PIPELINE_PLUGINS,
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": _build_system_prompt(project),
            },
        )

        prompt = _build_turn_prompt(user_message, history or [])

        async for message in query(prompt=prompt, options=options):
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
                from .llm_usage import extract_usage, record_usage
                record_usage(
                    source="engineer_chat", usage=extract_usage(message),
                    project_code=getattr(project, "project_code", None),
                )
                yield {
                    "type": "chat_complete",
                    "cost_usd": message.total_cost_usd,
                    "duration_ms": message.duration_ms,
                    "session_id": message.session_id,
                    "is_error": message.is_error,
                }

    except ImportError:
        yield {
            "type": "error",
            "message": "claude-agent-sdk is not installed.",
        }
    except Exception as e:
        yield {"type": "error", "message": str(e)}
