#!/usr/bin/env python3
"""
Generate the "Unified MCP endpoint" presentation diagram.

Run: env/bin/python docs/diagrams/mcp_unified_endpoint.py
"""

from __future__ import annotations

from pathlib import Path

from mcp_diagram_lib import (
    H,
    MONO,
    W,
    canvas,
    circle,
    line,
    multiline,
    path,
    pill,
    rect,
    save_svg,
    text,
)


TOOL_CLASSES = [
    (
        "READ",
        "6 tools",
        ["list_projects", "get_project_state", "get_plan_summary", "get_stage_results", "run_cypher", "get_dbt_project"],
        "#2563EB",
        "#EFF6FF",
    ),
    (
        "MECHANICAL",
        "7 tools",
        [
            "set_data_source",
            "set_serving_mode",
            "set_materialization_target",
            "accept_request",
            "reset_stage",
            "complete_stage",
            "get_stage_config_options",
        ],
        "#64748B",
        "#F8FAFC",
    ),
    (
        "INTERACTIVE",
        "2 tools",
        ["get_pending_questions", "answer_question"],
        "#7C3AED",
        "#FAF5FF",
    ),
    (
        "REVIEW-WRITE",
        "5 tools",
        [
            "review_description",
            "review_mapping",
            "review_domain_rule",
            "review_table_description",
            "review_relationship_description",
        ],
        "#E11D48",
        "#FFF1F2",
    ),
    (
        "AGENTIC",
        "2 tools",
        ["run_stage", "query_semantic_layer"],
        "#16A34A",
        "#ECFDF5",
    ),
]

SKILLS = [
    "data-discovery",
    "data-discovery-to-dcat-neo4j",
    "data-profiling",
    "data-profiling-to-dqv-neo4j",
    "metadata-enrichment",
    "column-name-standardizer",
    "data-quality-rule-generation",
    "data-mapping-neo4j",
    "odcs-to-graph",
    "data-serving-virtual-view",
    "data-product-deployment-reflector",
    "data-scoring",
    "data-quality-failure-analysis",
    "data-remediation-planning",
    "playbook-reflector",
]


def client_tile(elements: list[str], x: int, y: int, title: str, subtitle: str, detail: str, accent: str) -> None:
    elements.append(rect(x, y, 210, 68, fill="#FFFFFF", stroke="#D8E2EE", stroke_width=1.4, rx=8, filter_="url(#tight-shadow)"))
    elements.append(rect(x, y, 5, 68, fill=accent, rx=4))
    elements.append(circle(x + 22, y + 22, 9, fill=accent, opacity=0.18))
    elements.append(circle(x + 22, y + 22, 4, fill=accent))
    elements.append(text(x + 40, y + 21, title, size=13, weight=800, fill="#0F172A"))
    elements.append(text(x + 40, y + 40, subtitle, size=10.5, weight=600, fill="#475569"))
    elements.append(text(x + 40, y + 56, detail, size=9, weight=500, fill="#64748B"))


def tool_card(elements: list[str], x: int, y: int, w: int, h: int, title: str, count: str, tools: list[str], accent: str, fill: str) -> None:
    elements.append(rect(x, y, w, h, fill=fill, stroke="#D8E2EE", stroke_width=1.1, rx=8, filter_="url(#tight-shadow)"))
    elements.append(rect(x, y, w, 34, fill=accent, rx=8))
    elements.append(rect(x, y + 22, w, 12, fill=accent, rx=0))
    elements.append(text(x + 14, y + 18, title, size=12, weight=800, fill="#FFFFFF"))
    elements.append(text(x + w - 14, y + 18, count, size=10, weight=800, fill="#E0F2FE", anchor="end"))

    start_y = y + 52
    line_h = 16 if len(tools) <= 5 else 14
    for idx, tool in enumerate(tools):
        elements.append(circle(x + 17, start_y + idx * line_h - 1, 3.2, fill=accent, opacity=0.85))
        elements.append(text(x + 27, start_y + idx * line_h, tool, size=9.4 if len(tool) < 28 else 8.2, family=MONO, weight=650, fill="#1E293B"))


def skill_chip(elements: list[str], x: float, y: float, w: float, label: str) -> None:
    elements.append(rect(x, y, w, 25, fill="#FFFFFF", stroke="#86EFAC", stroke_width=1.2, rx=12.5))
    elements.append(text(x + w / 2, y + 13, label, size=9.2 if len(label) < 28 else 8.2, family=MONO, weight=700, fill="#14532D", anchor="middle"))


def build() -> None:
    elements: list[str] = []
    elements.extend(
        canvas(
            "Unified MCP Endpoint",
            "One Workbench service exposes every capability to Claude Code, IDE agents, OpenAI Codex, and other MCP clients.",
            accent="#22C55E",
        )
    )

    # Client side.
    elements.append(rect(48, 124, 268, 430, fill="#FFFFFF", stroke="#C7D2FE", stroke_width=1.5, rx=10, filter_="url(#shadow)"))
    elements.append(rect(48, 124, 268, 42, fill="#EEF2FF", stroke="#C7D2FE", stroke_width=1, rx=10))
    elements.append(rect(48, 150, 268, 16, fill="#EEF2FF", rx=0))
    elements.append(text(70, 147, "MCP clients", size=14, weight=850, fill="#312E81"))
    elements.append(text(290, 147, "anywhere", size=10, weight=800, fill="#6366F1", anchor="end"))
    client_tile(elements, 76, 188, "Claude Code", "engineer workflow", "project-local MCP config", "#2563EB")
    client_tile(elements, 76, 272, "IDE agent", "editor-integrated", "same endpoint + token", "#7C3AED")
    client_tile(elements, 76, 356, "OpenAI Codex", "agentic coding client", "streamable HTTP MCP", "#0891B2")
    elements.extend(pill(76, 462, 210, 30, "client skill: workbench-guide", fill="#FFFBEB", stroke="#F59E0B", text_fill="#92400E", size=10.2))
    elements.extend(
        multiline(
            86,
            510,
            ["Orchestrates the lifecycle.", "Does not run server skills locally."],
            size=9.5,
            fill="#64748B",
            weight=600,
            line_height=15,
        )
    )

    # Endpoint gateway.
    gateway_x, gateway_y, gateway_w, gateway_h = 362, 154, 330, 360
    elements.append(rect(gateway_x, gateway_y, gateway_w, gateway_h, fill="#0F172A", stroke="#38BDF8", stroke_width=2.4, rx=12, filter_="url(#endpoint-glow)"))
    elements.append(text(gateway_x + gateway_w / 2, gateway_y + 44, "Data Workbench MCP", size=19, weight=850, fill="#FFFFFF", anchor="middle"))
    elements.append(text(gateway_x + gateway_w / 2, gateway_y + 72, "FastMCP control plane mounted at /mcp", size=11.5, weight=600, fill="#BAE6FD", anchor="middle"))
    elements.append(circle(gateway_x + gateway_w / 2, gateway_y + 150, 72, fill="#0B1220", stroke="#38BDF8", stroke_width=2.2, opacity=0.96))
    elements.append(text(gateway_x + gateway_w / 2, gateway_y + 132, "/mcp", size=34, weight=900, fill="#FFFFFF", anchor="middle", family=MONO))
    elements.append(text(gateway_x + gateway_w / 2, gateway_y + 164, "22 tools", size=16, weight=800, fill="#7DD3FC", anchor="middle"))
    elements.append(text(gateway_x + gateway_w / 2, gateway_y + 190, "one authenticated endpoint", size=10.5, weight=700, fill="#CBD5E1", anchor="middle"))
    elements.extend(pill(gateway_x + 34, gateway_y + 250, 120, 28, "bearer token", fill="#172554", stroke="#60A5FA", text_fill="#BFDBFE", size=10))
    elements.extend(pill(gateway_x + 176, gateway_y + 250, 120, 28, "project ACL", fill="#052E2B", stroke="#2DD4BF", text_fill="#99F6E4", size=10))
    elements.extend(pill(gateway_x + 34, gateway_y + 292, 262, 28, "server-side trust boundary", fill="#1E1B4B", stroke="#A78BFA", text_fill="#DDD6FE", size=10))
    elements.append(text(gateway_x + gateway_w / 2, gateway_y + 340, "UI parity lives behind this same service", size=10, weight=650, fill="#94A3B8", anchor="middle"))

    # Tool surface.
    surface_x, surface_y, surface_w, surface_h = 730, 124, 662, 430
    elements.append(rect(surface_x, surface_y, surface_w, surface_h, fill="#FFFFFF", stroke="#CBD5E1", stroke_width=1.5, rx=10, filter_="url(#shadow)"))
    elements.append(rect(surface_x, surface_y, surface_w, 42, fill="#F8FAFC", stroke="#CBD5E1", stroke_width=1, rx=10))
    elements.append(rect(surface_x, surface_y + 26, surface_w, 16, fill="#F8FAFC", rx=0))
    elements.append(text(surface_x + 22, surface_y + 23, "Tool surface by execution class", size=14, weight=850, fill="#0F172A"))
    elements.append(text(surface_x + surface_w - 22, surface_y + 23, "read / mutate / ask / review / run", size=10.5, weight=700, fill="#64748B", anchor="end"))

    x0, y0 = surface_x + 18, surface_y + 62
    tool_card(elements, x0, y0, 198, 154, *TOOL_CLASSES[0])
    tool_card(elements, x0 + 214, y0, 198, 154, *TOOL_CLASSES[1])
    tool_card(elements, x0 + 428, y0, 198, 154, *TOOL_CLASSES[2])
    tool_card(elements, x0, y0 + 178, 306, 166, *TOOL_CLASSES[3])
    tool_card(elements, x0 + 320, y0 + 178, 306, 166, *TOOL_CLASSES[4])
    elements.append(rect(x0 + 340, y0 + 274, 266, 52, fill="#FFFBEB", stroke="#D97706", stroke_width=1.5, rx=8))
    elements.append(text(x0 + 473, y0 + 292, "AGENT HARNESS", size=13, weight=900, fill="#92400E", anchor="middle"))
    elements.append(text(x0 + 473, y0 + 312, "sub-agent + one skill per run", size=9.5, weight=700, fill="#B45309", anchor="middle"))

    # Flow lines.
    elements.append(path("M 316 342 C 334 342 342 342 362 342", stroke="#2563EB", stroke_width=3, marker="arrow-blue"))
    elements.append(text(338, 324, "MCP over HTTP", size=9.5, fill="#2563EB", weight=750, anchor="middle"))
    elements.append(path("M 692 342 C 708 342 714 342 730 342", stroke="#64748B", stroke_width=3, marker="arrow-slate"))
    elements.append(text(711, 324, "dispatch", size=9.5, fill="#64748B", weight=750, anchor="middle"))

    # Non-agentic state callout.
    elements.append(rect(428, 548, 200, 50, fill="#FFFFFF", stroke="#CBD5E1", stroke_width=1.2, rx=8, filter_="url(#tight-shadow)"))
    elements.append(text(528, 566, "direct backend writes", size=11, weight=850, fill="#334155", anchor="middle"))
    elements.append(text(528, 584, "Neo4j graph + run state", size=9.4, weight=600, fill="#64748B", anchor="middle"))
    elements.append(path("M 532 514 C 532 526 532 534 532 548", stroke="#64748B", stroke_width=2.2, marker="arrow-slate", dash="5 5"))

    # Skills pool.
    pool_x, pool_y, pool_w, pool_h = 48, 620, 1344, 152
    elements.append(rect(pool_x, pool_y, pool_w, pool_h, fill="#ECFDF5", stroke="#16A34A", stroke_width=2, rx=12, filter_="url(#shadow)"))
    elements.append(rect(pool_x, pool_y, pool_w, 42, fill="#16A34A", rx=12))
    elements.append(rect(pool_x, pool_y + 26, pool_w, 16, fill="#16A34A", rx=0))
    elements.append(text(pool_x + 22, pool_y + 23, "Agent Runtime Skills Pool", size=15, weight=900, fill="#FFFFFF"))
    elements.append(text(pool_x + pool_w - 22, pool_y + 23, "the same server-side skills used by the web UI", size=10.5, weight=750, fill="#DCFCE7", anchor="end"))

    cols, gap = 5, 14
    chip_w = (pool_w - 44 - (cols - 1) * gap) / cols
    for idx, skill in enumerate(SKILLS):
        row, col = divmod(idx, cols)
        skill_chip(elements, pool_x + 22 + col * (chip_w + gap), pool_y + 58 + row * 32, chip_w, skill)

    elements.append(path("M 1190 568 C 1190 594 1190 598 1190 620", stroke="#16A34A", stroke_width=3.4, marker="arrow-green"))
    elements.append(text(1110, 594, "agentic tools load skills", size=10, weight=850, fill="#15803D", anchor="middle"))

    save_svg(
        str(Path(__file__).with_suffix(".svg")),
        elements,
        title="Unified MCP Endpoint",
        desc="Clients connect to one Data Workbench MCP endpoint, which exposes tools by execution class and invokes server-side skills through an agent harness.",
    )


if __name__ == "__main__":
    build()
