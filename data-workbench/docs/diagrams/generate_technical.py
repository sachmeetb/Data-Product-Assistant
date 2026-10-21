#!/usr/bin/env python3
"""Technical data-agent architecture — claudecodedash Data Workbench end-to-end.

Three tiers (Client / Backend & Agent harness / State & Data) with a numbered
1-10 walk-through that traces a stage execution from UI click to reflection
loop. Skill hex cluster and reflection inset on the right.
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

TIER_BG_CLIENT = "#FAF5FF"
TIER_BG_BACKEND = "#F0FDFA"
TIER_BG_DATA = "#FFF7ED"

WALK_STROKE = "#9333EA"  # bold purple for numbered walk-through arrows


def tier_band(E, S, key, x, y, w, h, title, color, bg):
    S[key], e = rect(x, y, w, h, "",
                     bg=bg, stroke=color, stroke_width=2,
                     stroke_style="dashed", roundness=12)
    E.extend(e)
    _, e = text(x + 14, y + 8, title, font_size=14, color=color)
    E.extend(e)


def num_marker(E, x, y, n):
    """Draw a small numbered circle at (x, y) for walk-through traces."""
    _, e = ellipse(x - 14, y - 14, 28, 28, str(n),
                   bg=WALK_STROKE, stroke=WALK_STROKE,
                   text_color="#FFFFFF", font_size=14)
    E.extend(e)


def diagram():
    E = []
    S = {}

    # ── Header ────────────────────────────────────────────────────────────
    _, e = text(30, 22,
                "Data Agent — Technical Architecture (claudecodedash Data Workbench)",
                font_size=28, color="#111827")
    E.extend(e)
    _, e = text(30, 60,
                "Three tiers: Client · Backend & Agent harness · State & Data.  "
                "Numbered ① → ⑩ trace one stage execution end-to-end.",
                font_size=13, color=SLATE_S)
    E.extend(e)
    _, e = text(30, 84,
                "Sources: workbench/backend/{archetypes.py, sdk_runner.py, chat_runner.py, pipeline.py, main.py}, "
                "CLAUDE.md, architecture.md, ~/.claude/skills/*/SKILL.md",
                font_size=10, color=SLATE_S)
    E.extend(e)

    # ── Tier 1 — Client ───────────────────────────────────────────────────
    T1_Y, T1_H = 110, 240
    tier_band(E, S, "tier_client", 20, T1_Y, 1620, T1_H,
              "① Client tier — React + Vite SPA  (workbench/frontend/)",
              "#7E22CE", TIER_BG_CLIENT)

    # PO shell
    S["po_shell"], e = rect(50, T1_Y + 40, 770, 175,
                            "Product Workbench  (shells/ProductShell.tsx)",
                            bg="#FFFFFF", stroke="#7E22CE", stroke_width=2,
                            font_size=14, roundness=8)
    E.extend(e)
    po_surfaces = [
        ("New Product Wizard", 70, T1_Y + 80),
        ("Product Chat Drawer", 250, T1_Y + 80),
        ("My Products", 430, T1_Y + 80),
        ("Marketplace", 610, T1_Y + 80),
        ("ODCS Editor", 70, T1_Y + 130),
        ("Edit Diff Banner", 250, T1_Y + 130),
        ("Apply Cards", 430, T1_Y + 130),
        ("Persona Dashboard", 610, T1_Y + 130),
    ]
    for label, sx, sy in po_surfaces:
        S[f"po_{label}"], e = rect(sx, sy, 170, 40, label,
                                   bg="#FAF5FF", stroke="#7E22CE", stroke_width=1,
                                   font_size=11, roundness=8)
        E.extend(e)

    # Engineer shell
    S["eng_shell"], e = rect(840, T1_Y + 40, 780, 175,
                             "Engineering Workbench  (shells/EngineerShell.tsx)",
                             bg="#FFFFFF", stroke="#7E22CE", stroke_width=2,
                             font_size=14, roundness=8)
    E.extend(e)
    eng_surfaces = [
        ("Pipeline View", 860, T1_Y + 80),
        ("Stage Detail", 1040, T1_Y + 80),
        ("Review Panels (5)", 1220, T1_Y + 80),
        ("Chat Drawer", 1410, T1_Y + 80),
        ("Artifact Browser", 860, T1_Y + 130),
        ("Results Viewer", 1040, T1_Y + 130),
        ("Mapping Graph", 1220, T1_Y + 130),
        ("Project Dashboard", 1410, T1_Y + 130),
    ]
    for label, sx, sy in eng_surfaces:
        S[f"eng_{label}"], e = rect(sx, sy, 170, 40, label,
                                    bg="#FAF5FF", stroke="#7E22CE", stroke_width=1,
                                    font_size=11, roundness=8)
        E.extend(e)

    # Channels (REST + WebSocket)
    S["ch_rest"], e = rect(700, T1_Y + 220, 200, 30,
                           "REST  (axios)",
                           bg="#FFFFFF", stroke=SLATE_S, stroke_width=1,
                           font_size=12, roundness=8)
    E.extend(e)
    S["ch_ws"], e = rect(910, T1_Y + 220, 360, 30,
                         "WebSocket  (useWebSocket · useChatSocket · useProductChatSocket)",
                         bg="#FFFFFF", stroke=SLATE_S, stroke_width=1,
                         font_size=11, roundness=8)
    E.extend(e)

    # ── Tier 2 — Backend / Agent harness ─────────────────────────────────
    T2_Y, T2_H = 380, 510
    tier_band(E, S, "tier_backend", 20, T2_Y, 1620, T2_H,
              "② Backend & Agent harness — FastAPI + Python 3.12  (workbench/backend/)",
              EMERALD_S, TIER_BG_BACKEND)

    # FastAPI app
    S["fastapi"], e = rect(50, T2_Y + 40, 290, 80,
                           "FastAPI app\nmain.py",
                           bg="#FFFFFF", stroke=EMERALD_S, stroke_width=2,
                           font_size=13, roundness=8, font_family=3)
    E.extend(e)

    # Routers strip
    routers_text = (
        "routers/  ·  archetypes · stages · websocket · chat · reviews · summary · scoring\n"
        "marketplace · odcs · agent_messages · product_chat · osi · domain_catalogs · skills"
    )
    S["routers"], e = rect(360, T2_Y + 40, 720, 80, routers_text,
                           bg="#FFFFFF", stroke=EMERALD_S, stroke_width=1,
                           font_size=11, roundness=8, font_family=3)
    E.extend(e)

    # Pipeline orchestrator
    S["pipeline"], e = rect(50, T2_Y + 140, 510, 90,
                            "Pipeline Orchestrator\npipeline.py  +  archetypes.py "
                            "(STAGE_REGISTRY · _WF_* · DEPENDENCY_GRAPH · build_prompt)",
                            bg="#FFFFFF", stroke=EMERALD_S, stroke_width=2,
                            font_size=12, roundness=8, font_family=3)
    E.extend(e)

    # SDK runner
    S["sdk_runner"], e = rect(50, T2_Y + 250, 360, 170,
                              "sdk_runner.run_stage_streaming",
                              bg="#FFFFFF", stroke=EMERALD_S, stroke_width=2,
                              font_size=14, roundness=8, font_family=3)
    E.extend(e)
    _, e = text(60, T2_Y + 285,
                "claude_code_sdk.query()\n"
                "ALLOWED_TOOLS = [Read, Write, Edit,\n"
                "                 Bash, Glob, Grep, Skill]\n"
                "permission_mode = acceptEdits\n"
                "max_turns = 100\n"
                "+ anti-exploration system prompt\n"
                "+ scripts/agent_ask.py instructions",
                font_size=10, color="#111827", font_family=3)
    E.extend(e)

    # Chat runner
    S["chat_runner"], e = rect(430, T2_Y + 250, 360, 170,
                               "chat_runner.run_chat_turn",
                               bg="#FFFFFF", stroke=EMERALD_S, stroke_width=2,
                               font_size=14, roundness=8, font_family=3)
    E.extend(e)
    _, e = text(440, T2_Y + 285,
                "claude_code_sdk.query()\n"
                "ALLOWED_TOOLS = [Read, Bash,\n"
                "                 Grep, Glob, Skill]\n"
                "max_turns = 15  ·  READ-ONLY\n"
                "Embedded Neo4j creds in prompt\n"
                "Forbidden: read workbench.db,\n"
                "  grep secrets, brute-force",
                font_size=10, color="#111827", font_family=3)
    E.extend(e)

    # Backend-driven non-LLM stages
    S["non_llm"], e = rect(810, T2_Y + 250, 240, 170,
                           "Backend-driven\n(non-LLM)",
                           bg="#FFFFFF", stroke=EMERALD_S, stroke_width=1,
                           stroke_style="dashed", font_size=12, roundness=8,
                           font_family=3)
    E.extend(e)
    _, e = text(820, T2_Y + 295,
                "dq_test_executor.py\ndq_test_runs.py\n\n"
                "→ subprocess: skill scripts\n   (5/30 min ceilings)\n"
                "→ writes :TestRun /\n   :TestResult to Neo4j",
                font_size=10, color="#111827", font_family=3)
    E.extend(e)

    # Agent-to-user messaging loop
    S["agent_ask"], e = rect(50, T2_Y + 440, 740, 60,
                             "Agent-to-user messaging  ·  message_queue.py + routers/agent_messages.py + scripts/agent_ask.py  "
                             "(parks HTTP, relays via WebSocket)",
                             bg="#FFFFFF", stroke=EMERALD_S, stroke_width=1,
                             stroke_style="dashed", font_size=11, roundness=8,
                             font_family=3)
    E.extend(e)

    # LLM call circle
    S["llm"], e = ellipse(1080, T2_Y + 285, 160, 100,
                          "LLM call\nAnthropic API\n(via Claude Code SDK,\nmodel per host config)",
                          bg="#FFFFFF", stroke=EMERALD_S, stroke_width=2,
                          font_size=11)
    E.extend(e)

    # MCP bus (host environment)
    S["mcp"], e = rect(810, T2_Y + 440, 430, 60,
                       "MCP servers (host environment)  ·  consumed by skills via Bash/Skill tool",
                       bg="#FFFFFF", stroke=EMERALD_S, stroke_width=1,
                       stroke_style="dashed", font_size=11, roundness=8)
    E.extend(e)

    # Skills hex cluster (right column)
    SKILLS_X = 1270
    SKILLS_Y = T2_Y + 40
    S["skills_box"], e = rect(SKILLS_X - 10, SKILLS_Y - 5, 360, 460,
                              "",
                              bg="#FFFFFF", stroke=EMERALD_S, stroke_width=2,
                              stroke_style="dashed", roundness=12)
    E.extend(e)
    _, e = text(SKILLS_X, SKILLS_Y + 5,
                "⬡ Skills Library  ~/.claude/skills/  (portable, harness-agnostic)",
                font_size=11, color=EMERALD_S)
    E.extend(e)

    skills_grid = [
        # Pipeline skills (left → right, top → bottom in a 2-col grid)
        "data-discovery", "data-profiling",
        "discovery-to-dcat-neo4j", "profiling-to-dqv-neo4j",
        "metadata-enrichment", "data-quality-rule-generation",
        "domain-rule-enhancement", "data-mapping-neo4j",
        "data-scoring", "data-quality-testing-gx",
        "data-quality-testing-python", "data-quality-failure-analysis",
        "data-remediation-analysis", "data-remediation-planning",
        "data-serving-virtual-view", "column-name-standardizer",
        "project-chat-assistant", "product-authoring-assistant",
        "data-product-{discovery,", "schema, osi, name}-advisor",
        "data-product-spec-writer", "odcs-to-graph",
        "skill-reflector", "chat-reflector",
    ]
    skill_w, skill_h = 165, 32
    for i, skl in enumerate(skills_grid):
        col = i % 2
        row = i // 2
        sx = SKILLS_X + col * (skill_w + 5)
        sy = SKILLS_Y + 30 + row * (skill_h + 5)
        S[f"sk_{i}"], e = rect(sx, sy, skill_w, skill_h, skl,
                               bg="#FAFAF9", stroke=EMERALD_S, stroke_width=1,
                               font_size=9, roundness=8, font_family=3)
        E.extend(e)

    # ── Tier 3 — State & Data ────────────────────────────────────────────
    T3_Y, T3_H = 920, 280
    tier_band(E, S, "tier_data", 20, T3_Y, 1620, T3_H,
              "③ State & Data tier",
              AMBER_S, TIER_BG_DATA)

    # workbench.db
    S["sqlite"], e = rect(50, T3_Y + 40, 360, 220,
                          "workbench.db  (SQLite · SQLModel)",
                          bg="#FFFFFF", stroke=AMBER_S, stroke_width=2,
                          font_size=13, roundness=8, font_family=3)
    E.extend(e)
    _, e = text(60, T3_Y + 75,
                "Project · Workflow · AppSettings\n"
                "StageRun · StageExecution\n"
                "  (event stream ≤2MB, truncated flag)\n"
                "DQTestRun\n"
                "ChatSession · ChatMessage\n"
                "ProductRequest\n"
                "ProductChatSession · ProductChatMessage\n",
                font_size=10, color="#111827", font_family=3)
    E.extend(e)

    # Neo4j — System of Record for the agent system
    S["neo4j"], e = rect(430, T3_Y + 40, 460, 220,
                         "Neo4j Knowledge Graph — SYSTEM OF RECORD\n"
                         "(project-scoped via URI {project_code})",
                         bg="#FFFFFF", stroke=AMBER_S, stroke_width=3,
                         font_size=13, roundness=8, font_family=3)
    E.extend(e)
    _, e = text(440, T3_Y + 90,
                ":Project · :Catalog · :Dataset · :Column\n"
                ":DataContract · :DProdColumn · :DProdOutputDataset\n"
                ":PropertyShape · :ColumnDescription · :ColumnMapping\n"
                ":QualityScore · :TestRun · :TestResult\n"
                ":Activity · :Entity  (PROV-O)\n\n"
                "Vocabularies: DCAT-2 · DQV · SHACL-inspired · PROV-O\n"
                "Persistent state of every agent action — read AND write.",
                font_size=10, color="#111827", font_family=3)
    E.extend(e)

    # Source systems
    S["source_db"], e = rect(910, T3_Y + 40, 250, 100,
                             "Source Systems\nPostgreSQL · MySQL",
                             bg="#FFFFFF", stroke=AMBER_S, stroke_width=2,
                             font_size=13, roundness=8, font_family=3)
    E.extend(e)

    # Filesystem
    S["fs"], e = rect(910, T3_Y + 160, 250, 100,
                      "Filesystem\nprojects/{code}/  ·  playbook/\nartifacts · YAML · cypher",
                      bg="#FFFFFF", stroke=AMBER_S, stroke_width=2,
                      font_size=12, roundness=8, font_family=3)
    E.extend(e)

    # Reflection / learning loop inset
    S["reflect"], e = rect(1180, T3_Y + 40, 440, 220,
                           "Reflection / Learning Loop",
                           bg="#FFFFFF", stroke=ROSE_S, stroke_width=2,
                           stroke_style="dashed", font_size=13, roundness=8)
    E.extend(e)
    _, e = text(1190, T3_Y + 75,
                "1. StageExecution / ChatMessage rows\n"
                "2. scripts/reflect_on_skills.py  ·  reflect_on_chats.py\n"
                "3. skill-reflector  ·  chat-reflector  skills\n"
                "4. proposals → playbook/skill_reflections/\n"
                "                    playbook/chat_reflections/\n"
                "5. Human review → SKILL.md  ·  archetypes.py edits",
                font_size=10, color="#111827", font_family=3)
    E.extend(e)

    # ── Walk-through arrows (① → ⑩) ──────────────────────────────────────
    # 1. UI click → REST/WS channel
    aid, ae = arrow(S["eng_shell"], S["ch_ws"],
                    [(1230, T1_Y + 215), (1090, T1_Y + 220)],
                    stroke=WALK_STROKE, stroke_width=2)
    E.extend(ae); bind(E, aid, S["eng_shell"], S["ch_ws"])
    num_marker(E, 1240, T1_Y + 200, 1)

    # 2. WebSocket → FastAPI / routers
    aid, ae = arrow(S["ch_ws"], S["routers"],
                    [(1090, T1_Y + 250), (720, T2_Y + 40)],
                    stroke=WALK_STROKE, stroke_width=2,
                    label="run_stage")
    E.extend(ae); bind(E, aid, S["ch_ws"], S["routers"])
    num_marker(E, 900, T2_Y + 5, 2)

    # 3. routers → pipeline orchestrator
    aid, ae = arrow(S["routers"], S["pipeline"],
                    [(720, T2_Y + 120), (305, T2_Y + 140)],
                    stroke=WALK_STROKE, stroke_width=2,
                    label="STAGE_REGISTRY lookup")
    E.extend(ae); bind(E, aid, S["routers"], S["pipeline"])
    num_marker(E, 510, T2_Y + 130, 3)

    # 4. pipeline → sdk_runner (build_prompt + skill-load directive)
    aid, ae = arrow(S["pipeline"], S["sdk_runner"],
                    [(230, T2_Y + 230), (230, T2_Y + 250)],
                    stroke=WALK_STROKE, stroke_width=2,
                    label="build_prompt()")
    E.extend(ae); bind(E, aid, S["pipeline"], S["sdk_runner"])
    num_marker(E, 230, T2_Y + 240, 4)

    # 5. sdk_runner → LLM
    aid, ae = arrow(S["sdk_runner"], S["llm"],
                    [(410, T2_Y + 335), (1080, T2_Y + 335)],
                    stroke=WALK_STROKE, stroke_width=2)
    E.extend(ae); bind(E, aid, S["sdk_runner"], S["llm"])
    num_marker(E, 750, T2_Y + 320, 5)

    # 6. agent loads SKILL.md from skills cluster
    aid, ae = arrow(S["llm"], S["skills_box"],
                    [(1240, T2_Y + 335), (SKILLS_X - 10, T2_Y + 200)],
                    stroke=WALK_STROKE, stroke_width=2,
                    label="load SKILL.md")
    E.extend(ae); bind(E, aid, S["llm"], S["skills_box"])
    num_marker(E, 1255, T2_Y + 250, 6)

    # 7. skill (via Bash) → Source DB or Neo4j
    aid, ae = arrow(S["skills_box"], S["source_db"],
                    [(1450, T2_Y + 460), (1035, T3_Y + 90)],
                    stroke=WALK_STROKE, stroke_width=2,
                    label="Bash / scripts")
    E.extend(ae); bind(E, aid, S["skills_box"], S["source_db"])
    aid, ae = arrow(S["skills_box"], S["neo4j"],
                    [(1300, T2_Y + 460), (660, T3_Y + 40)],
                    stroke=WALK_STROKE, stroke_width=2,
                    label="run_cypher.py")
    E.extend(ae); bind(E, aid, S["skills_box"], S["neo4j"])
    num_marker(E, 1240, T2_Y + 480, 7)

    # 8. streaming events back: sdk_runner → routers → WS → UI
    aid, ae = arrow(S["sdk_runner"], S["ch_ws"],
                    [(230, T2_Y + 250), (230, T1_Y + 230), (1000, T1_Y + 235)],
                    stroke=WALK_STROKE, stroke_width=2,
                    stroke_style="dashed",
                    label="streaming events")
    E.extend(ae); bind(E, aid, S["sdk_runner"], S["ch_ws"])
    num_marker(E, 230, T1_Y + 250, 8)

    # 9. persisted to workbench.db + Neo4j
    aid, ae = arrow(S["sdk_runner"], S["sqlite"],
                    [(140, T2_Y + 420), (140, T3_Y + 40)],
                    stroke=WALK_STROKE, stroke_width=2,
                    stroke_style="dotted",
                    label="StageExecution.log_json")
    E.extend(ae); bind(E, aid, S["sdk_runner"], S["sqlite"])
    num_marker(E, 140, T3_Y + 20, 9)

    # 10. reflection loop: workbench.db → reflection inset
    aid, ae = arrow(S["sqlite"], S["reflect"],
                    [(410, T3_Y + 130), (1180, T3_Y + 130)],
                    stroke=WALK_STROKE, stroke_width=2,
                    stroke_style="dashed",
                    label="reflect_on_skills.py / reflect_on_chats.py")
    E.extend(ae); bind(E, aid, S["sqlite"], S["reflect"])
    num_marker(E, 1170, T3_Y + 130, 10)

    # ── Auxiliary arrows (chat_runner path) ──────────────────────────────
    aid, ae = arrow(S["routers"], S["chat_runner"],
                    [(720, T2_Y + 120), (610, T2_Y + 250)],
                    stroke=EMERALD_S, stroke_width=1, stroke_style="dotted",
                    label="chat / product-chat")
    E.extend(ae); bind(E, aid, S["routers"], S["chat_runner"])

    # chat_runner → run_cypher (Neo4j)
    aid, ae = arrow(S["chat_runner"], S["neo4j"],
                    [(610, T2_Y + 420), (660, T3_Y + 40)],
                    stroke=EMERALD_S, stroke_width=1, stroke_style="dotted",
                    label="run_cypher.py (read-only)")
    E.extend(ae); bind(E, aid, S["chat_runner"], S["neo4j"])

    # FastAPI → workbench.db (state R/W)
    aid, ae = arrow(S["fastapi"], S["sqlite"],
                    [(195, T2_Y + 120), (195, T2_Y + 140)],
                    stroke=AMBER_S, stroke_width=1, stroke_style="dotted")
    E.extend(ae); bind(E, aid, S["fastapi"], S["sqlite"])

    # Backend non-LLM → Neo4j
    aid, ae = arrow(S["non_llm"], S["neo4j"],
                    [(930, T2_Y + 420), (700, T3_Y + 40)],
                    stroke=EMERALD_S, stroke_width=1, stroke_style="dotted",
                    label=":TestRun")
    E.extend(ae); bind(E, aid, S["non_llm"], S["neo4j"])

    # Skill → MCP (tool surface usage)
    aid, ae = arrow(S["skills_box"], S["mcp"],
                    [(SKILLS_X, T2_Y + 460), (1100, T2_Y + 470)],
                    stroke=EMERALD_S, stroke_width=1, stroke_style="dashed")
    E.extend(ae); bind(E, aid, S["skills_box"], S["mcp"])

    # Agent-ask loop ↔ WebSocket
    aid, ae = arrow(S["agent_ask"], S["ch_ws"],
                    [(420, T2_Y + 440), (1090, T1_Y + 250)],
                    stroke=ROSE_S, stroke_width=1, stroke_style="dashed",
                    label="agent_ask.py ↔ user")
    E.extend(ae); bind(E, aid, S["agent_ask"], S["ch_ws"])

    # ── Legend ──────────────────────────────────────────────────────────
    S["legend"], e = rect(20, 1210, 480, 65,
                          "",
                          bg="#FFFFFF", stroke=SLATE_S, stroke_width=1,
                          font_size=11, roundness=8)
    E.extend(e)
    _, e = text(30, 1218,
                "Legend:  bold purple ① → ⑩ traces a stage execution end-to-end.\n"
                "→ invoke    ⇢ read (dashed)    ⇣ write (dotted)    rose dashed = HITL / ask",
                font_size=10, color="#111827")
    E.extend(e)

    # Materialization note
    S["mat_note"], e = rect(520, 1210, 700, 65,
                            "claudecodedash uses the SKILL materialization (loaded into Claude Code SDK).",
                            bg=EMERALD_BG, stroke=EMERALD_S, stroke_width=1,
                            font_size=11, roundness=8)
    E.extend(e)
    _, e = text(530, 1240,
                "The same capabilities could equally ship as MCP endpoints or as standalone agents — Skill-Agent Duality.",
                font_size=10, color=EMERALD_S)
    E.extend(e)

    # ── Footer ──────────────────────────────────────────────────────────
    _, e = text(1240, 1240,
                "Companion: conceptual / logical / business.excalidraw.   Rev: 2026-05-08.",
                font_size=10, color=SLATE_S)
    E.extend(e)

    save(E, "/home/niel/working/claudecodedash/docs/diagrams/data-agents-technical.excalidraw")


if __name__ == "__main__":
    diagram()
