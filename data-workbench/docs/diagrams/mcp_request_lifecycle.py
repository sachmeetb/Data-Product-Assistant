#!/usr/bin/env python3
"""
Generate the "MCP request lifecycle and skill status matrix" presentation diagram.

Run: env/bin/python docs/diagrams/mcp_request_lifecycle.py
"""

from __future__ import annotations

from pathlib import Path

from mcp_diagram_lib import (
    MONO,
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


GREEN = "#16A34A"
AMBER = "#F59E0B"

PHASES = [
    ("01", "Initialize request", "#2563EB"),
    ("02", "Agent harness", "#D97706"),
    ("03", "Skill execution", "#16A34A"),
    ("04", "Output / review", "#7C3AED"),
]

ROWS = [
    {
        "tool": "Data Discovery",
        "call": "run_stage",
        "skill": "data-discovery",
        "desc": "catalog a source DB",
        "accent": "#0EA5E9",
        "steps": [
            ("config + run_id", "tables selected", GREEN),
            ("load skill", "sub-agent starts", GREEN),
            ("connect + inspect", "schemas, tables, columns", GREEN),
            ("write catalog graph", "complete", GREEN),
        ],
    },
    {
        "tool": "Mapping & Transformation",
        "call": "run_stage",
        "skill": "data-mapping-neo4j",
        "desc": "source to product transforms",
        "accent": "#16A34A",
        "steps": [
            ("stage request", "run_id minted", GREEN),
            ("load skill", "mapping harness", GREEN),
            ("match columns", "author transforms", GREEN),
            ("write mappings", "awaiting_review", AMBER),
        ],
    },
    {
        "tool": "DQ Rule Generation",
        "call": "run_stage",
        "skill": "data-quality-rule-generation",
        "desc": "rules from profiling",
        "accent": "#DB2777",
        "steps": [
            ("stage request", "profiles available", GREEN),
            ("load skill", "rule generator", GREEN),
            ("scan profiles", "derive constraints", GREEN),
            ("write shapes", "complete", GREEN),
        ],
    },
    {
        "tool": "Serving - View",
        "call": "run_stage",
        "skill": "data-serving-virtual-view",
        "desc": "emit product view DDL",
        "accent": "#7C3AED",
        "steps": [
            ("stage request", "serving mode=virtual", GREEN),
            ("load skill", "view builder", GREEN),
            ("resolve joins", "emit SQL view", GREEN),
            ("serving definition", "complete", GREEN),
        ],
    },
    {
        "tool": "Semantic Query",
        "call": "query_semantic_layer",
        "skill": "marketplace-product-chat-assistant",
        "desc": "marketplace natural-language Q&A",
        "accent": "#F97316",
        "standalone": True,
        "steps": [
            ("domain + question", "retrieval mode", GREEN),
            ("concept plan", "entity resolution", GREEN),
            ("author SQL", "execute on views", GREEN),
            ("answer package", "markdown + table", GREEN),
        ],
    },
]

POOL_SKILLS = [
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


def phase_header(elements: list[str], x: int, y: int, w: int, num: str, title: str, color: str) -> None:
    elements.append(rect(x, y, w, 46, fill="#FFFFFF", stroke=color, stroke_width=1.5, rx=8, filter_="url(#tight-shadow)"))
    elements.append(circle(x + 24, y + 23, 13, fill=color))
    elements.append(text(x + 24, y + 23, num, size=10, weight=900, fill="#FFFFFF", anchor="middle", family=MONO))
    elements.append(text(x + 48, y + 23, title, size=12, weight=850, fill="#0F172A"))


def tool_cell(elements: list[str], x: int, y: int, w: int, h: int, row: dict) -> None:
    accent = row["accent"]
    elements.append(rect(x, y, w, h, fill="#FFFFFF", stroke="#D8E2EE", stroke_width=1.2, rx=8, filter_="url(#tight-shadow)"))
    elements.append(rect(x, y, 7, h, fill=accent, rx=4))
    elements.append(text(x + 20, y + 18, row["tool"], size=12.5, weight=900, fill="#0F172A"))
    elements.append(text(x + w - 16, y + 18, row["call"], size=8.2, weight=750, fill="#64748B", anchor="end", family=MONO))
    elements.append(text(x + 20, y + 36, row["desc"], size=9.2, weight=600, fill="#64748B"))
    if row.get("standalone"):
        elements.extend(pill(x + w - 92, y + 44, 76, 18, "standalone", fill="#FFF7ED", stroke="#FDBA74", text_fill="#C2410C", size=7.8))
    skill_x = x + 20
    skill_w = w - 118 if row.get("standalone") else w - 36
    elements.append(rect(skill_x, y + 44, skill_w, 18, fill="#ECFDF5", stroke="#86EFAC", stroke_width=1, rx=9))
    elements.append(text(skill_x + skill_w / 2, y + 53.5, row["skill"], size=7.0 if len(row["skill"]) > 29 else 8.2, weight=750, fill="#14532D", anchor="middle", family=MONO))


def phase_node(elements: list[str], x: int, y: int, w: int, h: int, step: tuple[str, str, str], phase_color: str) -> None:
    top, bottom, status = step
    fill = "#FFFBEB" if status == AMBER else "#FFFFFF"
    stroke = AMBER if status == AMBER else "#D8E2EE"
    elements.append(rect(x, y, w, h, fill=fill, stroke=stroke, stroke_width=1.2, rx=8))
    elements.append(circle(x + 18, y + 20, 5.5, fill=status, stroke="#FFFFFF", stroke_width=1))
    elements.append(text(x + 32, y + 18, top, size=9.5, weight=850, fill="#0F172A"))
    elements.append(text(x + 32, y + 38, bottom, size=8.8, weight=650, fill="#64748B"))
    elements.append(rect(x, y + h - 4, w, 4, fill=phase_color, rx=2, opacity=0.55))


def skill_chip(elements: list[str], x: float, y: float, w: float, label: str) -> None:
    elements.append(rect(x, y, w, 21, fill="#FFFFFF", stroke="#86EFAC", stroke_width=1, rx=10.5))
    elements.append(text(x + w / 2, y + 11, label, size=7.5 if len(label) > 28 else 8.1, weight=750, fill="#14532D", anchor="middle", family=MONO))


def build() -> None:
    elements: list[str] = []
    elements.extend(
        canvas(
            "MCP Request Lifecycle and Skill Status Matrix",
            "Agentic tools follow the same request phases; each row shows the server-side skill behind the tool.",
            accent="#22C55E",
        )
    )

    panel_x, panel_y, panel_w, panel_h = 48, 118, 1344, 512
    elements.append(rect(panel_x, panel_y, panel_w, panel_h, fill="#FFFFFF", stroke="#CBD5E1", stroke_width=1.5, rx=12, filter_="url(#shadow)"))
    elements.append(text(panel_x + 24, panel_y + 28, "Representative agentic paths", size=15, weight=900, fill="#0F172A"))
    elements.append(text(panel_x + panel_w - 24, panel_y + 28, "Initialize -> Harness -> Skill -> Output", size=10.5, weight=800, fill="#64748B", anchor="end", family=MONO))

    tool_x, tool_w = panel_x + 20, 260
    phase_x0, phase_w, phase_gap = 352, 240, 18
    header_y = panel_y + 54
    elements.append(rect(tool_x, header_y, tool_w, 46, fill="#0F172A", stroke="#0F172A", stroke_width=1, rx=8))
    elements.append(text(tool_x + 18, header_y + 23, "Tool / skill", size=12, weight=900, fill="#FFFFFF"))
    for idx, (num, phase, color) in enumerate(PHASES):
        phase_header(elements, phase_x0 + idx * (phase_w + phase_gap), header_y, phase_w, num, phase, color)

    row_y0, row_h, row_gap = header_y + 62, 66, 8
    for row_idx, row in enumerate(ROWS):
        ry = row_y0 + row_idx * (row_h + row_gap)
        elements.append(rect(panel_x + 20, ry - 5, panel_w - 40, row_h + 10, fill="#F8FAFC" if row_idx % 2 else "#FFFFFF", stroke="#E2E8F0", stroke_width=1, rx=9))
        tool_cell(elements, tool_x, ry, tool_w, row_h, row)
        mid_y = ry + row_h / 2
        elements.append(line(phase_x0 + 18, mid_y, phase_x0 + 3 * (phase_w + phase_gap) + phase_w - 18, mid_y, stroke="#CBD5E1", stroke_width=2))
        for idx, step in enumerate(row["steps"]):
            px = phase_x0 + idx * (phase_w + phase_gap)
            phase_node(elements, px, ry + 8, phase_w, 50, step, PHASES[idx][2])

    # Review status callout pinned to the data-mapping final node.
    map_y = row_y0 + 1 * (row_h + row_gap)
    callout_x, callout_y = 1124, map_y + 52
    elements.append(path(f"M {phase_x0 + 3 * (phase_w + phase_gap) + 208} {map_y + 47} C 1304 {map_y + 62} 1280 {callout_y} {callout_x + 190} {callout_y}", stroke="#F59E0B", stroke_width=2, marker="arrow-amber"))
    elements.append(rect(callout_x, callout_y - 17, 192, 34, fill="#FFFBEB", stroke="#F59E0B", stroke_width=1.3, rx=8, filter_="url(#tight-shadow)"))
    elements.append(text(callout_x + 96, callout_y, "review_* or UI clears the gate", size=9.5, weight=850, fill="#92400E", anchor="middle"))

    # Legend.
    legend_y = panel_y + panel_h - 18
    elements.append(circle(panel_x + 24, legend_y, 5.5, fill=GREEN, stroke="#FFFFFF", stroke_width=1))
    elements.append(text(panel_x + 38, legend_y, "complete / in normal flow", size=9.2, weight=700, fill="#475569"))
    elements.append(circle(panel_x + 206, legend_y, 5.5, fill=AMBER, stroke="#FFFFFF", stroke_width=1))
    elements.append(text(panel_x + 220, legend_y, "awaiting_review two-person gate", size=9.2, weight=700, fill="#475569"))

    # Skills pool.
    pool_x, pool_y, pool_w, pool_h = 48, 654, 1344, 120
    elements.append(rect(pool_x, pool_y, pool_w, pool_h, fill="#ECFDF5", stroke="#16A34A", stroke_width=2, rx=12, filter_="url(#shadow)"))
    elements.append(rect(pool_x, pool_y, pool_w, 34, fill="#16A34A", rx=12))
    elements.append(rect(pool_x, pool_y + 22, pool_w, 12, fill="#16A34A", rx=0))
    elements.append(text(pool_x + 22, pool_y + 18, "Modular server-side skills pool", size=13.5, weight=900, fill="#FFFFFF"))
    elements.append(text(pool_x + pool_w - 22, pool_y + 18, "run_stage resolves a skill from STAGE_REGISTRY; query_semantic_layer loads its marketplace skill directly", size=9.5, weight=750, fill="#DCFCE7", anchor="end"))

    cols, gap = 5, 12
    chip_w = (pool_w - 44 - (cols - 1) * gap) / cols
    for idx, skill in enumerate(POOL_SKILLS):
        r, c = divmod(idx, cols)
        skill_chip(elements, pool_x + 22 + c * (chip_w + gap), pool_y + 46 + r * 24, chip_w, skill)

    elements.append(text(720, 798, "The MCP tool definition is thin; the Workbench skill does the domain work behind the endpoint.", size=11, weight=850, fill="#0F172A", anchor="middle"))

    save_svg(
        str(Path(__file__).with_suffix(".svg")),
        elements,
        title="MCP Request Lifecycle and Skill Status Matrix",
        desc="A swimlane matrix showing agentic MCP tools moving through initialize, agent harness, skill execution, and output or review phases.",
    )


if __name__ == "__main__":
    build()
