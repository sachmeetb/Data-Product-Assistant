"""Product Workbench chat runner.

Sibling to :mod:`chat_runner` — same streaming shape, Claude Code SDK
invocation, and tool allowlist, but a wizard-authoring system prompt and
the ``product-authoring-assistant`` skill instead of the project-scoped
one. Unlike the DPE chat, the product chat does not require a project
to exist: the wizard may call us before a project row has been provisioned,
so ``project_code``/Neo4j credentials are optional.
"""

from __future__ import annotations

from typing import AsyncGenerator, Optional

from .config import BASE_DIR, PIPELINE_PLUGINS
from .models import Project


SKILL_NAME = "product-authoring-assistant"
TEMPLATE_SKILL_NAME = "template-authoring-assistant"
CHAT_ALLOWED_TOOLS = ["Read", "Bash", "Grep", "Glob", "Skill"]


def _skill_for_surface(surface: Optional[str]) -> str:
    """Pick the authoring skill by the chat surface. The Blueprint Library's
    template editor sends ``surface="template"`` in its context."""
    return TEMPLATE_SKILL_NAME if surface == "template" else SKILL_NAME


def _build_template_system_prompt() -> str:
    """System prompt for the Blueprint-Library template surface. There is no
    project/graph — the assistant works from the ODCS ``spec`` in the context
    block and the domain catalogs on disk, and returns Apply suggestions the PO
    drops straight into the editor."""
    return (
        "You are the Data Product Owner's co-author inside the **Blueprint "
        "Library** — a reusable data-product **spec template** editor (not a "
        "live project). The user is authoring or refining an ODCS template: a "
        "name, domain, description, purpose, and a single dataset's columns. "
        "The user's message includes a JSON context block with `surface: "
        "\"template\"`, the `spec` fields (name/domain/description/purpose/"
        "product_kind/dataset_name) and the current `columns` — trust it, don't "
        "re-fetch it.\n\n"
        f"FIRST every turn, load the `{TEMPLATE_SKILL_NAME}` skill via the Skill "
        "tool and follow its instructions (incl. when to invoke sub-skills like "
        "`data-product-name-advisor`).\n\n"
        "## Domain catalogs (on disk, readable)\n"
        f"{BASE_DIR}/playbook/domain_catalogs/common.yaml\n"
        f"{BASE_DIR}/playbook/domain_catalogs/hr.yaml\n"
        f"{BASE_DIR}/playbook/domain_catalogs/customer.yaml\n"
        f"{BASE_DIR}/playbook/domain_catalogs/finance.yaml\n"
        f"{BASE_DIR}/playbook/domain_catalogs/products_sales.yaml\n"
        f"{BASE_DIR}/playbook/domain_catalogs/retail banking.yaml\n\n"
        "## Hard rules\n"
        "- The name / description / purpose / column text you author is the DATA "
        "PRODUCT's OWN content — write it as if the product exists and is in use. "
        "Never call it a 'template', 'blueprint', 'reference', 'candidate', or "
        "'spec' in that content, and name who USES the product (not who 'builds "
        "from' it). That it's currently a template is metadata, not content.\n"
        "- READ-ONLY. You never write the graph or the template — your only "
        "effect is the text + suggestion blocks you return, which the PO applies "
        "by clicking Apply.\n"
        "- Templates are project-independent blueprints. Do NOT run Cypher, do "
        "NOT inspect workbench.db / .env, do NOT search for credentials. The "
        "context block + catalogs are all you need.\n"
        "- Only propose columns/values relevant to the template's domain; ground "
        "them in the catalogs where possible.\n"
        "- Stay terse. A two-sentence answer beats two paragraphs.\n"
    )


def _build_system_prompt(project: Optional[Project], surface: Optional[str] = None) -> str:
    """Build the system prompt for one product-chat turn.

    Before a project is provisioned (the wizard calls us during step 1-3)
    there is no graph context yet — the assistant works from the wizard
    state embedded in the user message and the domain catalog files on
    disk. Once the wizard has created a project (step 4+), we add Neo4j
    credentials and a canonical ``run_cypher.py`` invocation so the
    assistant can answer data-grounded questions.

    ``surface="template"`` swaps in the Blueprint-Library template prompt.
    """
    if surface == "template":
        return _build_template_system_prompt()
    base = (
        "You are the Data Product Owner's co-author inside the Product "
        "Workbench wizard. You help the user describe what they're trying "
        "to build, pick sensible names, shape the schema, and decide which "
        "quality rules to apply. The user's message will include a small "
        "JSON block describing the current wizard state (idea, domain, "
        "selected_columns, custom_columns, name, dataset_name, description, "
        "purpose) and the specific field / intent they clicked Guide-me on.\n\n"
        f"FIRST every turn, load the `{SKILL_NAME}` skill via the Skill tool. "
        "Follow its instructions — it tells you when to invoke sub-skills "
        "like `data-product-name-advisor`.\n\n"
        "## Domain catalogs (on disk, readable)\n"
        f"{BASE_DIR}/playbook/domain_catalogs/common.yaml\n"
        f"{BASE_DIR}/playbook/domain_catalogs/hr.yaml\n"
        f"{BASE_DIR}/playbook/domain_catalogs/customer.yaml\n"
        f"{BASE_DIR}/playbook/domain_catalogs/finance.yaml\n"
        f"{BASE_DIR}/playbook/domain_catalogs/products_sales.yaml\n"
        f"{BASE_DIR}/playbook/domain_catalogs/retail banking.yaml\n\n"
        "## Hard rules\n"
        "- READ-ONLY. Never modify the graph, never create projects, never "
        "advance wizard steps. Your effect on the world is the text you "
        "send back.\n"
        "- Do not invent graph data. If you need something that isn't in the "
        "user's message and isn't in the catalogs, say so and ask.\n"
        "- Do not inspect workbench.db, .env files, or search the codebase "
        "for credentials. The wizard state in the user message is all you "
        "need.\n"
        "- Stay terse. A two-sentence answer beats a two-paragraph answer.\n"
    )

    if project is None:
        return base + (
            "\n## No project yet\n"
            "The wizard has not yet provisioned a project — answer from the "
            "wizard state and the catalogs only. Do not attempt to run "
            "Cypher queries.\n"
        )

    return base + (
        "\n## Project context\n"
        f"- project_code: {project.project_code}\n"
        f"- domain: {project.domain or '(unset)'}\n"
        f"- archetype: {project.archetype}\n\n"
        "If the user asks something that requires data on the draft "
        "contract, you may run the project-chat-assistant's run_cypher.py "
        "with this project's Neo4j credentials. Keep reads scoped to "
        f"projectCode='{project.project_code}' — never query other projects.\n"
        f"- neo4j_host: {project.neo4j_host}\n"
        f"- neo4j_port: {project.neo4j_port}\n"
        f"- neo4j_user: {project.neo4j_user}\n"
        f"- neo4j_password: {project.neo4j_password}\n"
        f"- neo4j_database: {project.neo4j_database}\n"
    )


def _build_turn_prompt(user_message: str, history: list[dict], surface: Optional[str] = None) -> str:
    parts = [f"FIRST: Load the {_skill_for_surface(surface)} skill using the Skill tool."]
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


async def run_product_chat_turn(
    user_message: str,
    project: Optional[Project],
    cwd: str,
    history: Optional[list[dict]] = None,
    surface: Optional[str] = None,
) -> AsyncGenerator[dict, None]:
    """Stream one product-chat turn via the Claude Code SDK.

    Events mirror :func:`chat_runner.run_chat_turn` exactly so the frontend
    can reuse the same event-handling logic.
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
            cwd=cwd,
            max_turns=15,
            skills="all",
            plugins=PIPELINE_PLUGINS,
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": _build_system_prompt(project, surface),
            },
        )

        prompt = _build_turn_prompt(user_message, history or [], surface)

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
                    source="template_chat" if surface == "template" else "product_chat",
                    usage=extract_usage(message),
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
        yield {"type": "error", "message": "claude-agent-sdk is not installed."}
    except Exception as e:
        yield {"type": "error", "message": str(e)}
