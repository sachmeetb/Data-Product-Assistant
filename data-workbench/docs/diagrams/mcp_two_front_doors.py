#!/usr/bin/env python3
"""
Generate the "Two front doors, one skill set" presentation diagram.

Run: env/bin/python docs/diagrams/mcp_two_front_doors.py
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


CX = 720


def browser_panel(elements: list[str], x: int, y: int) -> None:
    elements.append(rect(x, y, 370, 180, fill="#FFFFFF", stroke="#93C5FD", stroke_width=1.6, rx=10, filter_="url(#shadow)"))
    elements.append(rect(x, y, 370, 34, fill="#DBEAFE", stroke="#93C5FD", stroke_width=1, rx=10))
    elements.append(rect(x, y + 22, 370, 12, fill="#DBEAFE", rx=0))
    for i, color in enumerate(["#EF4444", "#F59E0B", "#22C55E"]):
        elements.append(circle(x + 18 + i * 16, y + 17, 4.5, fill=color))
    elements.append(text(x + 185, y + 17, "Workbench Web UI", size=12.5, weight=850, fill="#1E3A8A", anchor="middle"))
    elements.append(text(x + 24, y + 58, "user-friendly path", size=15, weight=850, fill="#0F172A"))
    elements.append(text(x + 24, y + 80, "A human clicks Run Stage and watches streamed progress.", size=10.5, weight=600, fill="#475569"))
    stages = [("Data Discovery", "#2563EB"), ("Mapping", "#16A34A"), ("Review", "#E11D48")]
    for idx, (label, color) in enumerate(stages):
        sx = x + 24 + idx * 105
        elements.append(rect(sx, y + 104, 90, 34, fill="#F8FAFC", stroke=color, stroke_width=1.2, rx=7))
        elements.append(text(sx + 45, y + 121, label, size=8.8, weight=800, fill=color, anchor="middle"))
    elements.extend(pill(x + 24, y + 148, 190, 23, "event sink: WebSocket send", fill="#EFF6FF", stroke="#60A5FA", text_fill="#1D4ED8", size=9.2))


def mcp_panel(elements: list[str], x: int, y: int) -> None:
    elements.append(rect(x, y, 370, 180, fill="#FFFFFF", stroke="#C4B5FD", stroke_width=1.6, rx=10, filter_="url(#shadow)"))
    elements.append(rect(x, y, 370, 34, fill="#EDE9FE", stroke="#C4B5FD", stroke_width=1, rx=10))
    elements.append(rect(x, y + 22, 370, 12, fill="#EDE9FE", rx=0))
    elements.append(text(x + 185, y + 17, "MCP Client", size=12.5, weight=850, fill="#4C1D95", anchor="middle"))
    elements.append(text(x + 24, y + 58, "programmatic path", size=15, weight=850, fill="#0F172A"))
    elements.append(text(x + 24, y + 80, "Claude Code, an IDE, or Codex calls the same capability.", size=10.5, weight=600, fill="#475569"))
    elements.append(rect(x + 24, y + 101, 322, 42, fill="#111827", stroke="#312E81", stroke_width=1, rx=7))
    elements.append(text(x + 42, y + 116, "mcp__workbench__run_stage(...)", size=11, weight=750, fill="#E0E7FF", family=MONO))
    elements.append(text(x + 42, y + 134, "then poll get_project_state", size=9.5, weight=650, fill="#A5B4FC", family=MONO))
    elements.extend(pill(x + 24, y + 148, 154, 23, "event sink: no-op", fill="#F5F3FF", stroke="#A78BFA", text_fill="#5B21B6", size=9.2))
    elements.extend(pill(x + 190, y + 148, 156, 23, "status via read tools", fill="#F8FAFC", stroke="#CBD5E1", text_fill="#475569", size=9.2))


def spine_box(
    elements: list[str],
    y: int,
    h: int,
    title: str,
    lines: list[str],
    *,
    accent: str,
    fill: str,
    file_tag: str,
    title_fill: str = "#0F172A",
) -> None:
    x, w = 465, 510
    elements.append(rect(x, y, w, h, fill=fill, stroke=accent, stroke_width=1.8, rx=10, filter_="url(#tight-shadow)"))
    elements.append(rect(x, y, 8, h, fill=accent, rx=4))
    elements.append(text(x + 28, y + 22, title, size=15, weight=900, fill=title_fill))
    elements.append(text(x + w - 18, y + 22, file_tag, size=8.6, weight=700, fill="#64748B", anchor="end", family=MONO))
    elements.extend(multiline(x + 28, y + 45, lines, size=10.5, fill="#475569", weight=600, line_height=16))


def result_card(elements: list[str], x: int, y: int, title: str, subtitle: str, accent: str) -> None:
    elements.append(rect(x, y, 278, 48, fill="#FFFFFF", stroke=accent, stroke_width=1.4, rx=8, filter_="url(#tight-shadow)"))
    elements.append(circle(x + 23, y + 24, 8, fill=accent, opacity=0.2))
    elements.append(circle(x + 23, y + 24, 4, fill=accent))
    elements.append(text(x + 42, y + 19, title, size=11.5, weight=850, fill="#0F172A"))
    elements.append(text(x + 42, y + 36, subtitle, size=9.2, weight=600, fill="#64748B"))


def build() -> None:
    elements: list[str] = []
    elements.extend(
        canvas(
            "Two Front Doors, One Skill Set",
            "The browser and MCP enter differently, then converge on the exact same backend execution spine.",
            accent="#F59E0B",
        )
    )

    browser_panel(elements, 64, 126)
    mcp_panel(elements, 1006, 126)

    elements.extend(pill(CX - 120, 176, 240, 28, "same Workbench capability", fill="#FFFFFF", stroke="#CBD5E1", text_fill="#334155", size=10.5))
    elements.append(path("M 434 218 C 520 218 536 330 594 330", stroke="#2563EB", stroke_width=3, marker="arrow-blue"))
    elements.append(path("M 1006 218 C 920 218 904 330 846 330", stroke="#4F46E5", stroke_width=3, marker="arrow-indigo"))
    elements.append(text(528, 248, "WebSocket run_stage", size=10, weight=800, fill="#2563EB", anchor="middle"))
    elements.append(text(912, 248, "MCP run_stage tool", size=10, weight=800, fill="#4F46E5", anchor="middle"))

    spine_box(
        elements,
        324,
        68,
        "start_stage_run(event_sink)",
        ["resolves the stage, marks it running, returns a run_id", "the event sink is the only caller-specific parameter"],
        accent="#2563EB",
        fill="#EFF6FF",
        file_tag="stage_execution.py",
        title_fill="#1E3A8A",
    )
    elements.append(line(CX, 392, CX, 420, stroke="#64748B", stroke_width=2.7, marker="arrow-slate"))

    spine_box(
        elements,
        420,
        78,
        "build_prompt(stage_def, project)",
        ["prepends the directive to load the stage's skill first", "injects project config, table picks, and lifecycle context"],
        accent="#7C3AED",
        fill="#FAF5FF",
        file_tag="pipeline.py",
        title_fill="#4C1D95",
    )
    elements.append(line(CX, 498, CX, 526, stroke="#64748B", stroke_width=2.7, marker="arrow-slate"))

    elements.append(rect(438, 526, 564, 112, fill="#FFFBEB", stroke="#D97706", stroke_width=2.4, rx=12, filter_="url(#shadow)"))
    elements.append(rect(438, 526, 10, 112, fill="#D97706", rx=5))
    elements.append(text(CX, 552, "AGENT HARNESS", size=18, weight=950, fill="#92400E", anchor="middle"))
    elements.append(text(CX, 576, "claude_code_sdk sub-agent running server-side", size=11.5, weight=800, fill="#B45309", anchor="middle"))
    elements.append(text(CX, 600, "Allowed tools: Read, Write, Edit, Bash, Glob, Grep, Skill", size=10.5, weight=700, fill="#78350F", anchor="middle", family=MONO))
    elements.append(text(CX, 620, "anti-exploration prompt, project cwd, max_turns=100", size=10, weight=650, fill="#78350F", anchor="middle"))
    elements.append(text(984, 548, "sdk_runner.py", size=8.6, weight=700, fill="#B45309", anchor="end", family=MONO))
    elements.append(line(CX, 638, CX, 666, stroke="#64748B", stroke_width=2.7, marker="arrow-slate"))

    spine_box(
        elements,
        666,
        58,
        "one Workbench skill",
        ["same server-side skill whether invoked by UI or MCP"],
        accent="#16A34A",
        fill="#ECFDF5",
        file_tag="Skill tool",
        title_fill="#14532D",
    )

    # Event sink callouts.
    elements.append(path("M 465 358 C 360 358 310 322 250 306", stroke="#2563EB", stroke_width=2.1, marker="arrow-blue", dash="6 6"))
    elements.append(text(320, 356, "streamed progress", size=9.6, weight=800, fill="#2563EB", anchor="middle"))
    elements.append(path("M 975 358 C 1080 358 1130 322 1190 306", stroke="#4F46E5", stroke_width=2.1, marker="arrow-indigo", dash="6 6"))
    elements.append(text(1120, 356, "headless polling", size=9.6, weight=800, fill="#4F46E5", anchor="middle"))

    # Shared output surface.
    elements.append(path("M 720 724 C 720 738 444 734 330 742", stroke="#16A34A", stroke_width=2.1, marker="arrow-green"))
    elements.append(path("M 720 724 C 720 738 720 734 720 742", stroke="#16A34A", stroke_width=2.1, marker="arrow-green"))
    elements.append(path("M 720 724 C 720 738 996 734 1110 742", stroke="#16A34A", stroke_width=2.1, marker="arrow-green"))
    result_card(elements, 190, 742, "Neo4j knowledge graph", "catalog, DQV, lineage, PROV-O", "#16A34A")
    result_card(elements, 581, 742, "On-disk artifacts", "contracts, mappings, SQL, dbt", "#7C3AED")
    result_card(elements, 972, 742, "Review gates", "same approvals and provenance", "#E11D48")

    save_svg(
        str(Path(__file__).with_suffix(".svg")),
        elements,
        title="Two Front Doors, One Skill Set",
        desc="The web UI and MCP run_stage entry point converge on start_stage_run, build_prompt, the agent harness, and one Workbench skill.",
    )


if __name__ == "__main__":
    build()
