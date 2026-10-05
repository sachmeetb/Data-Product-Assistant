#!/usr/bin/env python3
"""Business-level data-agent architecture diagram.

Highest-level view: data agents as reusable building blocks; the knowledge graph
as the system's system-of-record (not just a data source); three modes of use
(standalone / deterministic workflow / autonomous agent system); three
materialization forms (agent / MCP endpoint / agent skill); HITL across all modes.
"""

import sys
sys.path.insert(0, "/home/niel/.claude/skills/excalidraw-draw/scripts")

from excalidraw_lib import rect, ellipse, diamond, arrow, text, bind, save


INDIGO_S = "#4F46E5"
EMERALD_S = "#059669"
AMBER_S = "#D97706"
SLATE_S = "#475569"
ROSE_S = "#E11D48"
SKY_S = "#0284C7"
VIOLET_S = "#7E22CE"

INDIGO_BG = "#EEF2FF"
EMERALD_BG = "#ECFDF5"
AMBER_BG = "#FFFBEB"
SLATE_BG = "#F1F5F9"
ROSE_BG = "#FFF1F2"
SKY_BG = "#F0F9FF"
VIOLET_BG = "#FAF5FF"


def diagram():
    E = []
    S = {}

    # ── Header ────────────────────────────────────────────────────────────
    _, e = text(30, 25, "Data Agents — Business View",
                font_size=34, color="#111827")
    E.extend(e)
    _, e = text(30, 72,
                "Reusable building blocks for governed, auditable, reusable data work",
                font_size=16, color=SLATE_S)
    E.extend(e)
    _, e = text(30, 95,
                "Sources: vault › Niel & Faheem 4-30-2026, NTSF Skills & Code-Gen Demo 3-10-2026, Niel & Alfredo 6-9-2025, "
                "Agentic AI Bias, From Claude Code Skills to Standalone Agents, Project Colorado 9-10-2025, Google planning 2-10-2026.",
                font_size=10, color=SLATE_S)
    E.extend(e)

    # ── (A) Application Domains band ─────────────────────────────────────
    _, e = text(30, 130, "Where data agents land", font_size=14, color=SKY_S)
    E.extend(e)
    domains = [
        ("Data Product\nEngineering", "discovery → contract → publish"),
        ("Semantic Layer\n& Governance", "ontologies, lineage, business glossary"),
        ("Data\nEngineering", "pipelines, transforms, mapping"),
        ("Data\nModernization", "legacy scan, repoint, migration"),
        ("Data Quality &\nObservability", "rules, tests, scoring, drift"),
    ]
    dom_w, dom_h, dom_gap = 364, 105, 16
    dom_x_start = 30
    for i, (title, sub) in enumerate(domains):
        x = dom_x_start + i * (dom_w + dom_gap)
        S[f"dom_{i}"], e = rect(x, 155, dom_w, dom_h, title,
                                bg=SKY_BG, stroke=SKY_S, stroke_width=2,
                                font_size=14, roundness=8)
        E.extend(e)
        _, e = text(x + 10, 230, sub, font_size=11, color=SKY_S)
        E.extend(e)

    # ── (B) Building Blocks row ──────────────────────────────────────────
    _, e = text(30, 285, "Data Agent Building Blocks  ·  reusable capability tiles",
                font_size=14, color=INDIGO_S)
    E.extend(e)
    blocks = [
        "Discovery", "Profiling", "Mapping",
        "Quality Rules", "Quality Testing",
        "Scoring", "Description /\nEnrichment",
        "Remediation", "Lineage",
        "Migration\nAnalysis",
    ]
    blk_w, blk_h, blk_gap = 175, 55, 14
    blk_x_start = 30
    for i, b in enumerate(blocks):
        x = blk_x_start + i * (blk_w + blk_gap)
        S[f"blk_{i}"], e = rect(x, 310, blk_w, blk_h, b,
                                bg=INDIGO_BG, stroke=INDIGO_S, stroke_width=1,
                                font_size=11, roundness=8)
        E.extend(e)

    # ── (C) Three Modes of Use — left column ─────────────────────────────
    MODE_X, MODE_Y, MODE_W = 30, 405, 550
    S["modes_box"], e = rect(MODE_X, MODE_Y, MODE_W, 340, "",
                             bg="#FFFFFF", stroke=INDIGO_S, stroke_width=1,
                             stroke_style="dashed", roundness=12)
    E.extend(e)
    _, e = text(MODE_X + 14, MODE_Y + 10, "Three Modes of Use",
                font_size=18, color=INDIGO_S)
    E.extend(e)
    _, e = text(MODE_X + 14, MODE_Y + 40,
                "Match the operating mode to the determinism of the work.",
                font_size=11, color=SLATE_S)
    E.extend(e)

    modes = [
        ("① Standalone",
         "single agent, one-shot task",
         "use when the path is well-defined  ·  if-statements, not reasoning",
         MODE_Y + 70),
        ("② Deterministic Workflow",
         "fixed pipeline of agents",
         "use for predictable, batch-like sequences  ·  archetype workflows",
         MODE_Y + 160),
        ("③ Autonomous Agent System",
         "goal-driven, multi-agent reasoning",
         "use when the path is unknown until runtime  ·  reasoning loop",
         MODE_Y + 250),
    ]
    for title, sub, desc, y in modes:
        S[f"mode_{y}"], e = rect(MODE_X + 20, y, MODE_W - 40, 80, title,
                                 bg=INDIGO_BG, stroke=INDIGO_S, stroke_width=2,
                                 font_size=14, roundness=8)
        E.extend(e)
        _, e = text(MODE_X + 35, y + 30, sub, font_size=12, color=INDIGO_S)
        E.extend(e)
        _, e = text(MODE_X + 35, y + 50, desc, font_size=10, color=SLATE_S)
        E.extend(e)
        # HITL marker on each mode
        _, e = ellipse(MODE_X + MODE_W - 100, y + 20, 70, 40, "+ HITL",
                       bg=ROSE_BG, stroke=ROSE_S, stroke_width=1,
                       text_color=ROSE_S, font_size=10)
        E.extend(e)
    _, e = text(MODE_X + 14, MODE_Y + 320,
                "Human-in-the-Loop applies in all three modes.",
                font_size=11, color=ROSE_S)
    E.extend(e)

    # ── (D) Knowledge Graph as System of Record — center column ─────────
    KG_X, KG_Y, KG_W, KG_H = 600, 405, 700, 340
    S["kg_box"], e = rect(KG_X, KG_Y, KG_W, KG_H, "",
                          bg=AMBER_BG, stroke=AMBER_S, stroke_width=3, roundness=12)
    E.extend(e)
    _, e = text(KG_X + 20, KG_Y + 14, "Knowledge Graph — System of Record",
                font_size=20, color=AMBER_S)
    E.extend(e)
    _, e = text(KG_X + 20, KG_Y + 46,
                "Not just a data source — the persistent state of every agent action.",
                font_size=12, color=AMBER_S)
    E.extend(e)

    # Big KG ellipse centerpiece
    S["kg_core"], e = ellipse(KG_X + 120, KG_Y + 80, 460, 160,
                              "schema · profiles · rules · mappings · scores\n"
                              "descriptions · lineage · reviews · contracts\n"
                              "provenance (PROV-O) · quality measurements",
                              bg="#FFFFFF", stroke=AMBER_S, stroke_width=2,
                              font_size=12)
    E.extend(e)
    _, e = text(KG_X + 140, KG_Y + 250,
                "Vocabularies: DCAT-2 · DQV · SHACL · PROV-O · DPROD · ODCS",
                font_size=11, color=AMBER_S)
    E.extend(e)
    _, e = text(KG_X + 140, KG_Y + 272,
                "Every agent action — read AND write — is grounded here.",
                font_size=11, color=SLATE_S)
    E.extend(e)
    _, e = text(KG_X + 140, KG_Y + 295,
                "vault › Niel & Faheem 4-30-2026, NTSF Skills & Code-Gen Demo",
                font_size=9, color=SLATE_S)
    E.extend(e)

    # Arrows: building blocks read AND write KG (bidirectional)
    aid, ae = arrow(S["blk_4"], S["kg_core"],
                    [(875, 365), (KG_X + 350, KG_Y + 80)],
                    stroke=AMBER_S, stroke_width=2, label="read · write")
    E.extend(ae); bind(E, aid, S["blk_4"], S["kg_core"])

    # ── (E) Three Materialization Forms — right column ───────────────────
    MAT_X, MAT_Y, MAT_W = 1320, 405, 360
    S["mat_box"], e = rect(MAT_X, MAT_Y, MAT_W, 340, "",
                           bg="#FFFFFF", stroke=EMERALD_S, stroke_width=1,
                           stroke_style="dashed", roundness=12)
    E.extend(e)
    _, e = text(MAT_X + 14, MAT_Y + 10, "Three Materialization Forms",
                font_size=18, color=EMERALD_S)
    E.extend(e)
    _, e = text(MAT_X + 14, MAT_Y + 40,
                "Same capability ships in any/all forms.",
                font_size=11, color=SLATE_S)
    E.extend(e)

    materializations = [
        ("As an Agent",
         "LLM + system prompt + tools\n+ knowledge corpus",
         "for autonomy & reach",
         MAT_Y + 70),
        ("As an MCP Endpoint",
         "Tool aggregator over MCP\nany IDE / workbench / CLI",
         "for tool reuse across hosts",
         MAT_Y + 160),
        ("As an Agent Skill",
         "SKILL.md + scripts + refs\nloaded into a host harness",
         "for embed-for-speed",
         MAT_Y + 250),
    ]
    for title, sub, why, y in materializations:
        S[f"mat_{y}"], e = rect(MAT_X + 20, y, MAT_W - 40, 80, title,
                                bg=EMERALD_BG, stroke=EMERALD_S, stroke_width=2,
                                font_size=14, roundness=8)
        E.extend(e)
        _, e = text(MAT_X + 35, y + 30, sub, font_size=11, color=EMERALD_S)
        E.extend(e)
        _, e = text(MAT_X + 35, y + 65, why, font_size=10, color=SLATE_S)
        E.extend(e)
    _, e = text(MAT_X + 14, MAT_Y + 320,
                "Skill-Agent Duality: embed for speed · extract for reach.",
                font_size=11, color=EMERALD_S)
    E.extend(e)

    # ── (F) Source & Consumer band ──────────────────────────────────────
    BOT_Y = 770
    S["sources"], e = rect(30, BOT_Y, 600, 90,
                           "Source Systems   ·   DBs · APIs · Files · Streams · Legacy platforms",
                           bg="#FFFFFF", stroke=AMBER_S, stroke_width=1,
                           stroke_style="dashed", font_size=13, roundness=8)
    E.extend(e)
    S["consumers"], e = rect(660, BOT_Y, 600, 90,
                             "Consumer Systems   ·   BI · Apps · Other Agents · Marketplaces",
                             bg="#FFFFFF", stroke=AMBER_S, stroke_width=1,
                             stroke_style="dashed", font_size=13, roundness=8)
    E.extend(e)
    # Arrows: sources up to building blocks; KG down to consumers
    aid, ae = arrow(S["sources"], S["blk_0"],
                    [(150, BOT_Y), (115, 365)],
                    stroke=AMBER_S, stroke_width=1, stroke_style="dashed")
    E.extend(ae); bind(E, aid, S["sources"], S["blk_0"])
    aid, ae = arrow(S["kg_core"], S["consumers"],
                    [(KG_X + 350, KG_Y + 240), (960, BOT_Y)],
                    stroke=AMBER_S, stroke_width=1, stroke_style="dashed",
                    label="data products · semantic layer hydration")
    E.extend(ae); bind(E, aid, S["kg_core"], S["consumers"])

    # ── (G) HITL gutter (top-right of bottom section) ────────────────────
    S["hitl"], e = rect(1280, BOT_Y, 380, 90,
                        "Human-in-the-Loop touchpoints",
                        bg=ROSE_BG, stroke=ROSE_S, stroke_width=2,
                        stroke_style="dashed", font_size=13, roundness=8)
    E.extend(e)
    _, e = text(1290, BOT_Y + 35,
                "review · approve · edit · escalate\n"
                "applies in all 3 modes  ·  every action carries PROV-O",
                font_size=11, color=ROSE_S)
    E.extend(e)

    # ── (H) claudecodedash example callout ──────────────────────────────
    S["ccd"], e = rect(30, 880, 1230, 110,
                       "Example instantiation — claudecodedash Data Workbench",
                       bg=VIOLET_BG, stroke=VIOLET_S, stroke_width=2,
                       font_size=14, roundness=8, font_family=3)
    E.extend(e)
    _, e = text(40, 920,
                "Materialization:   ⬡ agent skills (~/.claude/skills/)\n"
                "Mode:               ② deterministic workflow (archetype: dpe-cf, dpe-sa, dq, dd)\n"
                "System of record:   project-scoped Neo4j knowledge graph (DCAT · DQV · SHACL · PROV-O)\n"
                "HITL:               5 review surfaces — descriptions, mappings, domain rules, escalations, unmapped columns",
                font_size=11, color="#111827", font_family=3)
    E.extend(e)

    # ── (I) Vault label callouts (bottom-right) ─────────────────────────
    S["labels"], e = rect(1280, 880, 600, 110,
                          "Vault-grounded talking points",
                          bg="#FFFFFF", stroke=SLATE_S, stroke_width=1,
                          font_size=12, roundness=8)
    E.extend(e)
    _, e = text(1290, 915,
                '"Knowledge graph as system of record"\n'
                '"Deterministic, workflow, and autonomous modes"\n'
                '"Skill-Agent Duality: embed for speed, extract for reach"\n'
                '"Semantic layer hydration from agent-generated artifacts"\n'
                '"Human checkpoints: review, approve, escalate"',
                font_size=10, color=SLATE_S)
    E.extend(e)

    # ── (J) Legend + Footer ─────────────────────────────────────────────
    S["legend"], e = rect(30, 1010, 700, 60,
                          "",
                          bg="#FFFFFF", stroke=SLATE_S, stroke_width=1,
                          font_size=11, roundness=8)
    E.extend(e)
    _, e = text(40, 1020,
                "Legend:  blue = application domains    indigo = building blocks / modes\n"
                "         amber = data plane (KG = system of record)    emerald = materialization    rose = HITL",
                font_size=10, color="#111827")
    E.extend(e)
    _, e = text(750, 1040,
                "Companion: data-agents-conceptual / -logical / -technical.excalidraw.   Rev: 2026-05-08.",
                font_size=10, color=SLATE_S)
    E.extend(e)

    save(E, "/home/niel/working/claudecodedash/docs/diagrams/data-agents-business.excalidraw")


if __name__ == "__main__":
    diagram()
