#!/usr/bin/env python3
"""Logical data-agent architecture diagram.

Four-plane swim-lane view (Control / Execution / Data & Knowledge / Observability),
left User-Surface lane, right Human-in-the-Loop gutter, with claudecodedash
instantiation badges pinned beneath each lane.
"""

import sys
sys.path.insert(0, "/home/niel/.claude/skills/excalidraw-draw/scripts")

from excalidraw_lib import rect, ellipse, diamond, arrow, text, bind, save


INDIGO_S = "#4F46E5"
EMERALD_S = "#059669"
AMBER_S = "#D97706"
SLATE_S = "#475569"
ROSE_S = "#E11D48"

INDIGO_BG = "#EEF2FF"
EMERALD_BG = "#ECFDF5"
AMBER_BG = "#FFFBEB"
SLATE_BG = "#F1F5F9"
ROSE_BG = "#FFF1F2"

LANE_X_START = 220
LANE_X_END = 1660
LANE_W = LANE_X_END - LANE_X_START

USR_X_START = 20
USR_X_END = 200

HITL_X_START = 1680
HITL_X_END = 1900


def lane_band(E, S, key, x, y, w, h, title, color, bg):
    """Draw a swim-lane container with a title strip on the left."""
    S[key], e = rect(x, y, w, h, "",
                     bg=bg, stroke=color, stroke_width=2,
                     stroke_style="dashed", roundness=12)
    E.extend(e)
    _, e = text(x + 12, y + 10, title, font_size=14, color=color)
    E.extend(e)


def diagram():
    E = []
    S = {}

    # ── Header ────────────────────────────────────────────────────────────
    _, e = text(30, 25, "Data Agent — Logical Architecture",
                font_size=32, color="#111827")
    E.extend(e)
    _, e = text(30, 70,
                "Four planes (Control · Execution · Data & Knowledge · Observability), "
                "User Surfaces lane (L), Human-in-the-Loop gutter (R)",
                font_size=14, color=SLATE_S)
    E.extend(e)
    _, e = text(30, 92,
                "Sources: vault › AI Refinery Overview, Anatomy of an Autonomous Agent, "
                "Everything a Developer Needs to Know About MCP, From Claude Code Skills to Standalone Agents",
                font_size=10, color=SLATE_S)
    E.extend(e)

    # Modes of Use + Materialization Forms (top-right pair)
    S["modes_inset"], e = rect(1340, 25, 270, 80,
                               "Three Modes of Use",
                               bg="#FFFFFF", stroke=INDIGO_S, stroke_width=1,
                               font_size=12, roundness=8)
    E.extend(e)
    _, e = text(1350, 55, "Standalone  ·  Workflow  ·  Autonomous",
                font_size=10, color=INDIGO_S)
    E.extend(e)
    _, e = text(1350, 75, "+ HITL in all three\nvault › Agentic AI Bias, Niel & Alfredo",
                font_size=9, color=SLATE_S)
    E.extend(e)

    S["mat_inset"], e = rect(1620, 25, 260, 80,
                             "Three Materialization Forms",
                             bg="#FFFFFF", stroke=EMERALD_S, stroke_width=1,
                             font_size=12, roundness=8)
    E.extend(e)
    _, e = text(1630, 55, "Agent  ·  MCP Endpoint  ·  Skill",
                font_size=10, color=EMERALD_S)
    E.extend(e)
    _, e = text(1630, 75, "Skill-Agent Duality\nvault › From Claude Code Skills to Standalone Agents",
                font_size=9, color=SLATE_S)
    E.extend(e)

    # ── User Surfaces vertical lane (left) ────────────────────────────────
    USR_Y, USR_H = 130, 920
    S["usr_lane"], e = rect(USR_X_START, USR_Y, USR_X_END - USR_X_START, USR_H,
                            "", bg="#FFFFFF", stroke="#94A3B8", stroke_width=1,
                            stroke_style="dashed", roundness=12)
    E.extend(e)
    _, e = text(USR_X_START + 12, USR_Y + 8, "User Surfaces",
                font_size=13, color="#475569")
    E.extend(e)

    personas = [
        "Data Product Owner",
        "Data Engineer",
        "Data Quality Analyst",
        "Data Steward",
        "Reviewer",
    ]
    for i, p in enumerate(personas):
        S[f"persona_{i}"], e = rect(USR_X_START + 10, USR_Y + 50 + i * 60,
                                    160, 50, p,
                                    bg="#F8FAFC", stroke="#94A3B8", stroke_width=1,
                                    font_size=11, roundness=8)
        E.extend(e)

    # Surface boxes
    S["surface_chat"], e = rect(USR_X_START + 10, USR_Y + 380, 160, 70,
                                "Conversational\nsurface",
                                bg="#FFFFFF", stroke="#94A3B8", stroke_width=2,
                                font_size=12, roundness=8)
    E.extend(e)
    S["surface_struct"], e = rect(USR_X_START + 10, USR_Y + 470, 160, 70,
                                  "Structured\n(wizard / pipeline)",
                                  bg="#FFFFFF", stroke="#94A3B8", stroke_width=2,
                                  font_size=12, roundness=8)
    E.extend(e)

    # ── Lane 1: Control Plane (indigo) ────────────────────────────────────
    L1_Y, L1_H = 130, 200
    lane_band(E, S, "lane_control", LANE_X_START, L1_Y, LANE_W, L1_H,
              "Lane 1 — Control Plane", INDIGO_S, INDIGO_BG)

    ctrl_items = [
        ("Orchestrator", LANE_X_START + 30),
        ("Archetype\nRegistry", LANE_X_START + 320),
        ("Stage\nRegistry", LANE_X_START + 580),
        ("Dependency\nGraph", LANE_X_START + 840),
        ("Run-state\nStore", LANE_X_START + 1100),
    ]
    for label, x in ctrl_items:
        key = "ctrl_" + label.split("\n")[0].lower().replace(" ", "_")
        S[key], e = rect(x, L1_Y + 60, 250, 100, label,
                         bg="#FFFFFF", stroke=INDIGO_S, stroke_width=2,
                         font_size=15, roundness=8)
        E.extend(e)

    # ── Lane 2: Execution Plane (emerald) ─────────────────────────────────
    L2_Y, L2_H = 350, 290
    lane_band(E, S, "lane_exec", LANE_X_START, L2_Y, LANE_W, L2_H,
              "Lane 2 — Execution Plane", EMERALD_S, EMERALD_BG)

    # Top row: Agent Harness + Tool Surface + LLM
    S["exec_harness"], e = rect(LANE_X_START + 30, L2_Y + 50, 280, 90,
                                "Agent Harness\n(Claude Code SDK · Distiller · LangGraph)",
                                bg="#FFFFFF", stroke=EMERALD_S, stroke_width=2,
                                font_size=13, roundness=8)
    E.extend(e)
    S["exec_tools"], e = rect(LANE_X_START + 330, L2_Y + 50, 220, 90,
                              "Tool Surface\n(Read · Write · Bash · Skill)",
                              bg="#FFFFFF", stroke=EMERALD_S, stroke_width=2,
                              font_size=13, roundness=8)
    E.extend(e)
    S["exec_llm"], e = ellipse(LANE_X_START + 580, L2_Y + 50, 140, 90,
                               "LLM call",
                               bg="#FFFFFF", stroke=EMERALD_S, stroke_width=2,
                               font_size=13)
    E.extend(e)

    # Skills library hex grid (drawn as labeled rounded rectangles with hex marker)
    skills = [
        "Discovery", "Profiling", "Mapping",
        "Rule-Generation", "Scoring", "Testing",
        "Remediation", "Description-\nEnrichment", "Serving",
    ]
    skill_w, skill_h = 130, 50
    skill_x_start = LANE_X_START + 750
    skill_gap_x, skill_gap_y = 15, 10
    for i, s in enumerate(skills):
        col = i % 3
        row = i // 3
        sx = skill_x_start + col * (skill_w + skill_gap_x)
        sy = L2_Y + 50 + row * (skill_h + skill_gap_y)
        S[f"skill_{i}"], e = rect(sx, sy, skill_w, skill_h, s,
                                  bg="#FFFFFF", stroke=EMERALD_S, stroke_width=1,
                                  font_size=11, roundness=8)
        E.extend(e)
    _, e = text(skill_x_start, L2_Y + 35, "⬡  Skills Library  (portable, harness-agnostic)",
                font_size=12, color=EMERALD_S)
    E.extend(e)

    # MCP horizontal bus across bottom of lane 2
    S["mcp_bus"], e = rect(LANE_X_START + 30, L2_Y + 220, LANE_W - 60, 50,
                           "MCP Bus  —  servers expose tools to any agent harness  (\"universal USB cable\")",
                           bg="#FFFFFF", stroke=EMERALD_S, stroke_width=2,
                           stroke_style="dashed", font_size=12, roundness=8)
    E.extend(e)
    _, e = text(LANE_X_START + 30, L2_Y + 275,
                "vault › Everything a Developer Needs to Know About Model Context Protocol.md",
                font_size=9, color=SLATE_S)
    E.extend(e)

    # Arrow harness → tool → LLM
    aid, ae = arrow(S["exec_harness"], S["exec_tools"],
                    [(LANE_X_START + 310, L2_Y + 95), (LANE_X_START + 330, L2_Y + 95)],
                    stroke=EMERALD_S, stroke_width=1)
    E.extend(ae); bind(E, aid, S["exec_harness"], S["exec_tools"])
    aid, ae = arrow(S["exec_tools"], S["exec_llm"],
                    [(LANE_X_START + 550, L2_Y + 95), (LANE_X_START + 580, L2_Y + 95)],
                    stroke=EMERALD_S, stroke_width=1)
    E.extend(ae); bind(E, aid, S["exec_tools"], S["exec_llm"])
    # Arrow tool → MCP
    aid, ae = arrow(S["exec_tools"], S["mcp_bus"],
                    [(LANE_X_START + 440, L2_Y + 140), (LANE_X_START + 440, L2_Y + 220)],
                    stroke=EMERALD_S, stroke_width=1, stroke_style="dashed")
    E.extend(ae); bind(E, aid, S["exec_tools"], S["mcp_bus"])

    # ── Lane 3: Data & Knowledge Plane (amber) ────────────────────────────
    L3_Y, L3_H = 660, 200
    lane_band(E, S, "lane_data", LANE_X_START, L3_Y, LANE_W, L3_H,
              "Lane 3 — Data & Knowledge Plane", AMBER_S, AMBER_BG)

    data_items = [
        ("Knowledge Graph — System of Record\nDCAT · DQV · SHACL · PROV-O\n(persistent state of every agent action)",
         LANE_X_START + 30, 3),
        ("Data Products /\nODCS Contracts", LANE_X_START + 400, 2),
        ("Source Systems\nRDBMS · Files · APIs · Streams", LANE_X_START + 770, 2),
        ("Playbook /\nDomain Catalogs", LANE_X_START + 1140, 2),
    ]
    for label, x, sw in data_items:
        key = "data_" + label.split("/")[0].split("\n")[0].split("—")[0].strip().lower().replace(" ", "_")
        S[key], e = ellipse(x, L3_Y + 60, 340, 110, label,
                            bg="#FFFFFF", stroke=AMBER_S, stroke_width=sw,
                            font_size=11 if sw == 3 else 13)
        E.extend(e)

    # ── Lane 4: Observability & Governance (slate) ────────────────────────
    L4_Y, L4_H = 880, 170
    lane_band(E, S, "lane_obs", LANE_X_START, L4_Y, LANE_W, L4_H,
              "Lane 4 — Observability & Governance", SLATE_S, SLATE_BG)

    obs_items = [
        "Stage Execution\nLedger",
        "Chat Session\nLedger",
        "Quality Scores\n(tiered)",
        "Provenance\n(PROV-O)",
        "Reflection /\nLearning Loop",
    ]
    for i, label in enumerate(obs_items):
        x = LANE_X_START + 30 + i * 280
        S[f"obs_{i}"], e = rect(x, L4_Y + 50, 250, 100, label,
                                bg="#FFFFFF", stroke=SLATE_S, stroke_width=2,
                                font_size=13, roundness=8)
        E.extend(e)

    # ── Right HITL gutter ────────────────────────────────────────────────
    HITL_Y, HITL_H = 130, 920
    S["hitl_lane"], e = rect(HITL_X_START, HITL_Y, HITL_X_END - HITL_X_START, HITL_H,
                             "", bg=ROSE_BG, stroke=ROSE_S, stroke_width=2,
                             stroke_style="dashed", roundness=12)
    E.extend(e)
    _, e = text(HITL_X_START + 10, HITL_Y + 10, "Human-in-the-Loop",
                font_size=13, color=ROSE_S)
    E.extend(e)

    review_queues = [
        "Descriptions",
        "Mappings",
        "Domain Rules",
        "Transformation\nEscalations",
        "Unmapped\nColumns",
    ]
    for i, label in enumerate(review_queues):
        S[f"hitl_{i}"], e = rect(HITL_X_START + 10, HITL_Y + 60 + i * 100,
                                 200, 90, label,
                                 bg="#FFFFFF", stroke=ROSE_S, stroke_width=1,
                                 stroke_style="dashed", font_size=12, roundness=8)
        E.extend(e)

    S["hitl_reject"], e = rect(HITL_X_START + 10, HITL_Y + 580, 200, 110,
                               "Structured rejection\ncategories\n· missing_context\n· too_broad / too_narrow\n· duplicate / unclear",
                               bg="#FFFFFF", stroke=ROSE_S, stroke_width=1,
                               font_size=10, roundness=8)
    E.extend(e)
    _, e = text(HITL_X_START + 10, HITL_Y + 700,
                "Reviews & rejections\nfeed PROV-O activities\non the artifact.",
                font_size=10, color=ROSE_S)
    E.extend(e)

    # ── Cross-cutting arrows ─────────────────────────────────────────────
    # User → Control
    aid, ae = arrow(S["surface_struct"], S["ctrl_orchestrator"],
                    [(USR_X_END, USR_Y + 575), (LANE_X_START + 30, L1_Y + 110)],
                    stroke="#475569", stroke_width=1)
    E.extend(ae); bind(E, aid, S["surface_struct"], S["ctrl_orchestrator"])
    # Orchestrator → Stage Registry
    aid, ae = arrow(S["ctrl_orchestrator"], S["ctrl_stage"],
                    [(LANE_X_START + 280, L1_Y + 110), (LANE_X_START + 580, L1_Y + 110)],
                    stroke=INDIGO_S, stroke_width=1)
    E.extend(ae); bind(E, aid, S["ctrl_orchestrator"], S["ctrl_stage"])
    # Stage Registry → Skill (executes)
    aid, ae = arrow(S["ctrl_stage"], S["skill_0"],
                    [(LANE_X_START + 700, L1_Y + 160),
                     (LANE_X_START + 700, L2_Y + 50)],
                    stroke=INDIGO_S, stroke_width=2, label="executes")
    E.extend(ae); bind(E, aid, S["ctrl_stage"], S["skill_0"])
    # MCP → KG
    aid, ae = arrow(S["mcp_bus"], S["data_knowledge_graph"],
                    [(LANE_X_START + 200, L2_Y + 270),
                     (LANE_X_START + 200, L3_Y + 60)],
                    stroke=AMBER_S, stroke_width=1, stroke_style="dashed",
                    label="tool calls")
    E.extend(ae); bind(E, aid, S["mcp_bus"], S["data_knowledge_graph"])
    # MCP → Source Systems
    aid, ae = arrow(S["mcp_bus"], S["data_source_systems"],
                    [(LANE_X_START + 940, L2_Y + 270),
                     (LANE_X_START + 940, L3_Y + 60)],
                    stroke=AMBER_S, stroke_width=1, stroke_style="dashed")
    E.extend(ae); bind(E, aid, S["mcp_bus"], S["data_source_systems"])
    # Execution → Observability (every action logged)
    aid, ae = arrow(S["exec_harness"], S["obs_0"],
                    [(LANE_X_START + 170, L2_Y + 140),
                     (LANE_X_START + 170, L4_Y + 50)],
                    stroke=SLATE_S, stroke_width=1, stroke_style="dotted",
                    label="logs")
    E.extend(ae); bind(E, aid, S["exec_harness"], S["obs_0"])
    # Observability → Control (learning loop) — long curved feedback arrow
    aid, ae = arrow(S["obs_4"], S["ctrl_stage"],
                    [(LANE_X_START + 1280, L4_Y + 50),
                     (LANE_X_START + 1280, L4_Y - 30),
                     (LANE_X_START + 1700, L4_Y - 30),
                     (LANE_X_START + 1700, L1_Y + 110),
                     (LANE_X_START + 700, L1_Y + 110)],
                    stroke="#C2255C", stroke_width=2, stroke_style="dashed",
                    label="reflection feeds back into prompts/skills")
    E.extend(ae); bind(E, aid, S["obs_4"], S["ctrl_stage"])
    # HITL gates per lane (rose dashed)
    for src_key, hitl_key, src_y in [
        ("ctrl_stage", "hitl_0", L1_Y + 160),
        ("skill_0", "hitl_1", L2_Y + 100),
        ("data_data_products", "hitl_2", L3_Y + 100),
    ]:
        aid, ae = arrow(S[src_key], S[hitl_key],
                        [(LANE_X_END, src_y), (HITL_X_START, src_y)],
                        stroke=ROSE_S, stroke_width=1, stroke_style="dashed")
        E.extend(ae); bind(E, aid, S[src_key], S[hitl_key])

    # ── claudecodedash instantiation badges (bottom row) ─────────────────
    BADGE_Y = 1080
    _, e = text(220, BADGE_Y - 25,
                "Example instantiation — claudecodedash Data Workbench  (each badge maps to the lane above it)",
                font_size=12, color=EMERALD_S)
    E.extend(e)

    badges = [
        ("Control",
         "archetypes.py  ·  STAGE_REGISTRY  ·  DEPENDENCY_GRAPH  ·  ARCHETYPE_REGISTRY  ·  pipeline.py",
         LANE_X_START + 0, 1430),
        ("Execution",
         "sdk_runner.py  ·  chat_runner.py  ·  ~/.claude/skills/  ·  Claude Code SDK  ·  agent_ask.py",
         LANE_X_START + 0, 1430),
    ]
    # Single combined badge row spanning lanes — easier to read
    badge_specs = [
        (LANE_X_START + 0, 350, "Control",
         "archetypes.py · STAGE_REGISTRY · DEPENDENCY_GRAPH · ARCHETYPE_REGISTRY"),
        (LANE_X_START + 360, 350, "Execution",
         "sdk_runner.py · chat_runner.py · ~/.claude/skills/ · agent_ask.py"),
        (LANE_X_START + 720, 360, "Data & Knowledge",
         "Neo4j (project-scoped URIs) · Postgres/MySQL · playbook/ · projects/{code}/"),
        (LANE_X_START + 1090, 350, "Observability",
         "workbench.db (StageExecution, ChatMessage) · playbook/{skill,chat}_reflections/"),
    ]
    for x_off, w, lane, items in badge_specs:
        S[f"badge_{lane}"], e = rect(x_off, BADGE_Y, w, 100,
                                     f"{lane}\n{items}",
                                     bg=EMERALD_BG, stroke=EMERALD_S, stroke_width=2,
                                     font_size=11, roundness=8, font_family=3)
        E.extend(e)

    # ── Legend ──────────────────────────────────────────────────────────
    S["legend"], e = rect(20, 1080, 180, 100,
                          "Edge legend",
                          bg="#FFFFFF", stroke=SLATE_S, stroke_width=1,
                          font_size=12, roundness=8)
    E.extend(e)
    _, e = text(28, 1110,
                "→  invoke\n⇢  read (dashed)\n⇣  write (dotted)\n⇄  stream (double)",
                font_size=10, color="#111827")
    E.extend(e)

    # ── Footer ──────────────────────────────────────────────────────────
    _, e = text(30, 1200,
                "Sources: vault docs as cited.  Companion: data-agents-conceptual.excalidraw, data-agents-technical.excalidraw.  Rev: 2026-05-08.",
                font_size=10, color=SLATE_S)
    E.extend(e)

    save(E, "/home/niel/working/claudecodedash/docs/diagrams/data-agents-logical.excalidraw")


if __name__ == "__main__":
    diagram()
