#!/usr/bin/env python3
"""Conceptual data-agent architecture diagram.

Reference model: Accenture 3-tier (Orchestrator / Super / Utility) + four planes
(Control / Execution / Data & Knowledge / Observability). claudecodedash workbench
appears as one instantiation example.
"""

import sys
sys.path.insert(0, "/home/niel/.claude/skills/excalidraw-draw/scripts")

from excalidraw_lib import rect, ellipse, diamond, arrow, text, bind, save


# Plane palette — kept consistent across conceptual / logical / technical diagrams.
INDIGO_S = "#4F46E5"
EMERALD_S = "#059669"
AMBER_S = "#D97706"
SLATE_S = "#475569"
ROSE_S = "#E11D48"

INDIGO_BG = "#EEF2FF"
AMBER_BG = "#FFFBEB"
SLATE_BG = "#F1F5F9"
ROSE_BG = "#FFF1F2"
GREEN_BG = "#ECFDF5"


def diagram():
    E = []
    S = {}

    # ── Header ────────────────────────────────────────────────────────────
    E.extend([text(30, 30, "What Is a Data Agent?", font_size=36, color="#111827")[1][0]])
    # Note: text() returns (id, [el]) — extract element. To avoid that confusion,
    # use the documented form: id, els = text(...); E.extend(els)
    # But api-reference shows text returns (id, elements_list); above hack works.
    # Re-do cleanly:
    E.clear()
    S.clear()

    # Title
    _, e = text(30, 30, "What Is a Data Agent?", font_size=36, color="#111827")
    E.extend(e)
    _, e = text(30, 78, "Conceptual View  ·  Reference model + claudecodedash example",
                font_size=16, color=SLATE_S)
    E.extend(e)

    # Definition box
    S["def"], e = rect(30, 110, 870, 80,
                       "An autonomous or semi-autonomous LLM-powered system that connects to data sources,\n"
                       "decomposes natural-language queries, retrieves and analyzes data, and acts on insights.",
                       bg="#FFFFFF", stroke=SLATE_S, stroke_width=1, font_size=14,
                       text_color="#111827", roundness=8)
    E.extend(e)

    # Four property pills
    pill_labels = ["Reactive", "Proactive", "Social", "Continuously Learning"]
    pill_x = 920
    for i, label in enumerate(pill_labels):
        S[f"pill_{i}"], e = rect(pill_x + i * 175, 130, 165, 40, label,
                                 bg=INDIGO_BG, stroke=INDIGO_S, stroke_width=1,
                                 font_size=14, roundness=20)
        E.extend(e)

    # Header footnote
    _, e = text(30, 200,
                "Definition: vault › Building and Evaluating Data Agents.md     "
                "Properties: vault › The Anatomy of an Autonomous Agent.md",
                font_size=11, color=SLATE_S)
    E.extend(e)

    # ── Observability strip (right vertical band) ─────────────────────────
    S["obs_band"], e = rect(1660, 20, 240, 1000, "",
                            bg=SLATE_BG, stroke=SLATE_S, stroke_width=1,
                            stroke_style="dashed", roundness=8)
    E.extend(e)
    _, e = text(1690, 35, "Observability\n& Governance", font_size=15, color=SLATE_S)
    E.extend(e)

    obs_items = [
        ("Conversation Ledger", 100),
        ("Agent Metrics", 215),
        ("Quality Scores", 330),
        ("Human-in-the-Loop\nReviews", 445),
        ("Reflection /\nLearning Loop", 580),
    ]
    for label, y in obs_items:
        is_hitl = "Human" in label
        S[f"obs_{y}"], e = rect(
            1680, y, 200, 90, label,
            bg="#FFFFFF",
            stroke=ROSE_S if is_hitl else SLATE_S,
            stroke_width=2 if is_hitl else 1,
            stroke_style="dashed" if is_hitl else "solid",
            font_size=13, roundness=8)
        E.extend(e)

    _, e = text(1685, 990, "(see logical / technical\nfor concrete stores)",
                font_size=10, color=SLATE_S)
    E.extend(e)

    # ── 3-tier hierarchy (Control plane) ──────────────────────────────────
    # Orchestrator
    S["orch"], e = rect(730, 240, 280, 80,
                        "Orchestrator Agent",
                        bg=INDIGO_BG, stroke=INDIGO_S, stroke_width=3,
                        font_size=20, roundness=8)
    E.extend(e)
    _, e = text(770, 295, "task routing  ·  global workflow state",
                font_size=11, color=INDIGO_S)
    E.extend(e)

    # Super
    S["super"], e = rect(730, 360, 280, 80,
                         "Super Agent",
                         bg=INDIGO_BG, stroke=INDIGO_S, stroke_width=3,
                         font_size=20, roundness=8)
    E.extend(e)
    _, e = text(740, 415, "intent understanding  ·  goal decomposition",
                font_size=11, color=INDIGO_S)
    E.extend(e)

    # Arrow Orch -> Super
    aid, ae = arrow(S["orch"], S["super"],
                    [(870, 320), (870, 360)],
                    stroke=INDIGO_S, stroke_width=2)
    E.extend(ae)
    bind(E, aid, S["orch"], S["super"])

    # 5 Utility Agents
    util_labels = ["Discovery", "Profiling", "Mapping", "Scoring", "Quality Testing"]
    util_w, util_h, util_gap = 220, 90, 40
    util_y = 530
    util_total_w = 5 * util_w + 4 * util_gap  # 1260
    util_x_start = 200
    for i, label in enumerate(util_labels):
        x = util_x_start + i * (util_w + util_gap)
        S[f"util_{i}"], e = rect(x, util_y, util_w, util_h,
                                 label,
                                 bg=INDIGO_BG, stroke=INDIGO_S, stroke_width=2,
                                 font_size=18, roundness=8)
        E.extend(e)
        _, e = text(x + 50, util_y + util_h - 22, "Utility Agent",
                    font_size=11, color=INDIGO_S)
        E.extend(e)
        # Arrow from Super to each Utility
        aid, ae = arrow(S["super"], S[f"util_{i}"],
                        [(870, 440), (x + util_w / 2, util_y)],
                        stroke=INDIGO_S, stroke_width=1)
        E.extend(ae)
        bind(E, aid, S["super"], S[f"util_{i}"])

    # 3-tier footnote
    _, e = text(200, 635, "3-tier model: vault › AI Refinery Overview April 2025.md",
                font_size=11, color=SLATE_S)
    E.extend(e)

    # HITL diamond — perched between Super and Utilities (right side)
    S["hitl_diamond"], e = diamond(1240, 405, 200, 100,
                                   "Human review gate\napprove · edit · reject",
                                   bg=ROSE_BG, stroke=ROSE_S, font_size=12,
                                   text_color=ROSE_S)
    E.extend(e)
    # Arrows agents <-> human
    aid, ae = arrow(S["super"], S["hitl_diamond"],
                    [(1010, 400), (1240, 455)],
                    stroke=ROSE_S, stroke_width=1, stroke_style="dashed")
    E.extend(ae)
    bind(E, aid, S["super"], S["hitl_diamond"])
    aid, ae = arrow(S["hitl_diamond"], S["util_4"],
                    [(1340, 505), (1340, 530)],
                    stroke=ROSE_S, stroke_width=1, stroke_style="dashed")
    E.extend(ae)
    bind(E, aid, S["hitl_diamond"], S["util_4"])

    # Dotted lines from each tier into observability band
    for src in ["orch", "super", "util_4"]:
        # Get rough end-y for each
        if src == "orch":
            sy = 280
        elif src == "super":
            sy = 400
        else:
            sy = util_y + util_h // 2
        aid, ae = arrow(S[src], S["obs_215"],
                        [(1010 if src != "util_4" else 1460, sy), (1680, 260)],
                        stroke=SLATE_S, stroke_width=1, stroke_style="dotted")
        E.extend(ae)
        bind(E, aid, S[src], S["obs_215"])

    # ── Zoom-in callout: Inside any Utility Agent ─────────────────────────
    zoom_y = 685
    S["zoom_title_box"], e = rect(80, zoom_y, 1540, 50,
                                  "Inside any Utility Agent",
                                  bg="#FFFFFF", stroke=INDIGO_S, stroke_width=1,
                                  font_size=18, roundness=8)
    E.extend(e)

    # Pillar A: Task Management
    S["pillar_a"], e = rect(120, zoom_y + 60, 700, 100,
                            "Task Management",
                            bg="#FFFFFF", stroke=INDIGO_S, stroke_width=2,
                            font_size=18, roundness=8)
    E.extend(e)
    _, e = text(160, zoom_y + 110, "▸  Planning      ▸  Execution",
                font_size=14, color="#111827")
    E.extend(e)

    # Pillar B: Intelligence
    S["pillar_b"], e = rect(880, zoom_y + 60, 700, 100,
                            "Intelligence",
                            bg="#FFFFFF", stroke=INDIGO_S, stroke_width=2,
                            font_size=18, roundness=8)
    E.extend(e)
    _, e = text(910, zoom_y + 110,
                "▸  LLM      ▸  SLM      ▸  Memory      ▸  Tools",
                font_size=14, color="#111827")
    E.extend(e)

    # Dashed connector from Mapping (util_2, center) to zoom title
    aid, ae = arrow(S["util_2"], S["zoom_title_box"],
                    [(util_x_start + 2 * (util_w + util_gap) + util_w / 2, util_y + util_h),
                     (850, zoom_y)],
                    stroke=INDIGO_S, stroke_width=1, stroke_style="dashed")
    E.extend(ae)
    bind(E, aid, S["util_2"], S["zoom_title_box"])

    # Zoom footnote
    _, e = text(120, zoom_y + 165, "vault › The Anatomy of an Autonomous Agent.md",
                font_size=11, color=SLATE_S)
    E.extend(e)

    # ── Data & Knowledge plane band ───────────────────────────────────────
    dk_y = 880
    _, e = text(80, dk_y - 25, "Data & Knowledge Plane",
                font_size=16, color=AMBER_S)
    E.extend(e)
    cyl_specs = [
        ("Data Products /\nSemantic Layer", 80, 2),
        ("Knowledge Graph — System of Record\nDCAT · DQV · SHACL · PROV-O\n(read AND write — every agent action grounded here)", 600, 4),
        ("Source Systems\n(DBs · APIs · Files · Streams)", 1120, 2),
    ]
    for i, (label, x, sw) in enumerate(cyl_specs):
        S[f"cyl_{i}"], e = ellipse(x, dk_y, 480, 110, label,
                                   bg=AMBER_BG, stroke=AMBER_S, stroke_width=sw,
                                   font_size=12 if i == 1 else 14)
        E.extend(e)
        # Arrow up into utilities row (target the closest utility)
        target_util_idx = min(4, i * 2)
        target_x = util_x_start + target_util_idx * (util_w + util_gap) + util_w / 2
        aid, ae = arrow(S[f"cyl_{i}"], S[f"util_{target_util_idx}"],
                        [(x + 240, dk_y), (target_x, util_y + util_h)],
                        stroke=AMBER_S, stroke_width=1)
        E.extend(ae)
        bind(E, aid, S[f"cyl_{i}"], S[f"util_{target_util_idx}"])

    # ── Modes of Use + Materialization Forms (paired insets) ─────────────
    pat_y = 1010
    S["modes_box"], e = rect(310, pat_y, 380, 60,
                             "Three Modes of Use",
                             bg="#FFFFFF", stroke=INDIGO_S, stroke_width=1,
                             font_size=12, roundness=8)
    E.extend(e)
    _, e = text(320, pat_y + 35,
                "Standalone  ·  Workflow  ·  Autonomous   (+ HITL in all)",
                font_size=11, color=INDIGO_S)
    E.extend(e)

    S["mat_box"], e = rect(700, pat_y, 380, 60,
                           "Three Materialization Forms",
                           bg="#FFFFFF", stroke="#059669", stroke_width=1,
                           font_size=12, roundness=8)
    E.extend(e)
    _, e = text(710, pat_y + 35,
                "Agent  ·  MCP Endpoint  ·  Skill   (Skill-Agent Duality)",
                font_size=11, color="#059669")
    E.extend(e)

    # ── Legend (bottom-left) ──────────────────────────────────────────────
    S["legend"], e = rect(30, 1010, 270, 60,
                          "Edge legend",
                          bg="#FFFFFF", stroke=SLATE_S, stroke_width=1,
                          font_size=12, roundness=8)
    E.extend(e)
    _, e = text(40, 1040,
                "→ invoke   ⇢ read   ⇣ write   ⇄ stream",
                font_size=11, color="#111827")
    E.extend(e)

    # ── claudecodedash instantiation badge ────────────────────────────────
    S["badge"], e = rect(1090, 1010, 530, 60,
                         "Example instantiation — claudecodedash",
                         bg=GREEN_BG, stroke=EMERALD_S, stroke_width=2,
                         font_size=12, roundness=8)
    E.extend(e)
    _, e = text(1100, 1037,
                "Workflow mode  ·  Skill materialization  ·  Neo4j as system of record",
                font_size=11, color=EMERALD_S, font_family=3)
    E.extend(e)

    # ── Footer ────────────────────────────────────────────────────────────
    _, e = text(30, 1080,
                "Sources: vault docs as cited.  Companion: data-agents-logical.excalidraw, data-agents-technical.excalidraw.  Rev: 2026-05-08.",
                font_size=10, color=SLATE_S)
    E.extend(e)

    save(E, "/home/niel/working/claudecodedash/docs/diagrams/data-agents-conceptual.excalidraw")


if __name__ == "__main__":
    diagram()
