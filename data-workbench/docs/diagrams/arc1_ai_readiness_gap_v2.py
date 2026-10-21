#!/usr/bin/env python3
"""
arc1_ai_readiness_gap_v2.py
"We use AI to build the data foundation. So AI can build on it."
Three-zone: AI as the Builder (left) | Data Products bridge (center) | AI as the Consumer (right)
"""
import sys, os
sys.path.insert(0, os.path.expanduser("~/.claude/skills/svg-architect/scripts"))
from svg_lib import *

W, H = 1440, 810

LEFT_X  = 40;   LEFT_W  = 500
CTR_X   = 568;  CTR_W   = 304
RIGHT_X = 900;  RIGHT_W = 500
COL_Y   = 92;   COL_H   = 554
MID_Y   = COL_Y + COL_H // 2   # 369
FOOTER_Y = 662


def skill_bullet(E, xi, yi, title, desc, accent=P["skills_stroke"]):
    """Left-accent skill bullet — no box, just a bar + text."""
    E.extend(rect(xi, yi + 4, 3, 46, fill=accent, stroke="none", rx=1))
    E.extend(text(xi + 14, yi + 18, title,
                  font_size=11, font_weight="700", color=P["title"]))
    E.extend(text(xi + 14, yi + 34, desc,
                  font_size=9, color=P["body"]))


def usecase_bullet(E, xi, yi, title, desc, accent=P["ui_stroke"]):
    """Left-accent use-case bullet."""
    E.extend(rect(xi, yi + 4, 3, 58, fill=accent, stroke="none", rx=1))
    E.extend(text(xi + 14, yi + 18, title,
                  font_size=11, font_weight="700", color=P["title"]))
    E.extend(text(xi + 14, yi + 34, desc,
                  font_size=9, color=P["body"]))


def build():
    E = []

    # ── Header ──────────────────────────────────────────────────────────────
    E.extend(rect(0, 0, W, 82, fill="#0F172A", stroke="none", rx=0))
    E.extend(text(W // 2, 26,
                  "We use AI to build the data foundation. So AI can build on it.",
                  font_size=21, font_weight="700", color="#FFFFFF",
                  text_anchor="middle"))
    E.extend(text(W // 2, 56,
                  "AI agent innovation applied to the domain of data engineering — "
                  "building the certified foundation that enterprise AI depends on.",
                  font_size=10, color="#94A3B8", text_anchor="middle"))
    E.extend(rect(W // 2 - 70, 78, 140, 3, fill=P["skills_stroke"], stroke="none", rx=1))

    # ════════════════════════════════════════════════════════════════════════
    # LEFT PANEL — AI as the BUILDER
    # ════════════════════════════════════════════════════════════════════════
    E.extend(rect(LEFT_X, COL_Y, LEFT_W, COL_H, fill=P["skills_fill"],
                  stroke="#86EFAC", stroke_width=2, rx=10))
    E.extend(header_band(LEFT_X, COL_Y, LEFT_W, 36,
                         label="AI APPLIED TO DATA ENGINEERING",
                         fill=P["skills_stroke"], font_size=13, rx=10))
    E.extend(text(LEFT_X + 16, COL_Y + 52,
                  "Data Workbench applies AI agent innovation to the domain of data.",
                  font_size=10, color=P["body"]))
    E.extend(text(LEFT_X + 16, COL_Y + 67,
                  "Purpose-built agent skills that do the data engineering work.",
                  font_size=10, color=P["body"]))

    skills = [
        ("Schema Discovery",     "Agents scan every table, column, FK, index · hours not weeks"),
        ("Data Profiling",       "Statistical patterns, nulls, cardinality, anomaly detection"),
        ("Metadata Enrichment",  "AI writes business descriptions · human approves or corrects"),
        ("Intelligent Mapping",  "Source→target column mapping with full lineage · human reviews"),
        ("Quality Assurance",    "Domain rules from catalogs · observation rules from profiling"),
        ("Serve & Publish",      "Virtual views + ODCS contract + marketplace listing in days"),
    ]
    sy = COL_Y + 88
    for title, desc in skills:
        skill_bullet(E, LEFT_X + 16, sy, title, desc)
        sy_div = sy + 60
        E.extend(divider(LEFT_X + 18, sy_div, LEFT_X + LEFT_W - 18, sy_div,
                         color="#DCFCE7"))
        sy += 72

    # Bottom callout in left panel
    cally = COL_Y + COL_H - 56
    E.extend(rect(LEFT_X + 14, cally, LEFT_W - 28, 44,
                  fill="#DCFCE7", stroke="#86EFAC", rx=6))
    E.extend(text(LEFT_X + 28, cally + 16,
                  "Human-in-the-loop at every step  ·  Active Learning  ·  PROV-O Provenance",
                  font_size=9, font_weight="700", color="#14532D"))
    E.extend(text(LEFT_X + 28, cally + 32,
                  "Every approval improves future runs.  Every rejection trains the system.",
                  font_size=9, color="#16A34A"))

    # ── Arrows in the gaps ───────────────────────────────────────────────────
    E.extend(arrow(LEFT_X + LEFT_W + 4, MID_Y, CTR_X - 4, MID_Y,
                   color=P["skills_stroke"], stroke_width=2.5))
    E.extend(text((LEFT_X + LEFT_W + CTR_X) // 2, MID_Y - 12,
                  "builds", font_size=9, font_weight="700",
                  color=P["skills_stroke"], text_anchor="middle"))

    E.extend(arrow(CTR_X + CTR_W + 4, MID_Y, RIGHT_X - 4, MID_Y,
                   color=P["ui_stroke"], stroke_width=2.5))
    E.extend(text((CTR_X + CTR_W + RIGHT_X) // 2, MID_Y - 12,
                  "enables", font_size=9, font_weight="700",
                  color=P["ui_stroke"], text_anchor="middle"))

    # ════════════════════════════════════════════════════════════════════════
    # CENTER BRIDGE — Data Products
    # ════════════════════════════════════════════════════════════════════════
    E.extend(rect(CTR_X, COL_Y, CTR_W, COL_H, fill="#FFFBF0",
                  stroke="#D97706", stroke_width=2.5, rx=10))
    E.extend(header_band(CTR_X, COL_Y, CTR_W, 36,
                         label="DATA PRODUCTS", fill="#D97706", font_size=14, rx=10))
    E.extend(text(CTR_X + CTR_W // 2, COL_Y + 53,
                  "Output of agent work on data.",
                  font_size=9, color="#78350F", text_anchor="middle"))
    E.extend(text(CTR_X + CTR_W // 2, COL_Y + 68,
                  "Foundation enterprise AI depends on.",
                  font_size=9, color="#78350F", text_anchor="middle"))

    # Two product types
    hw = (CTR_W - 32 - 8) // 2   # 132
    sa_x = CTR_X + 16
    cf_x = CTR_X + 16 + hw + 8

    E.extend(rect(sa_x, COL_Y + 84, hw, 108, fill="#DCFCE7", stroke="#86EFAC", rx=6))
    E.extend(text(sa_x + hw // 2, COL_Y + 102, "SOURCE-ALIGNED",
                  font_size=9, font_weight="700", color="#14532D", text_anchor="middle"))
    E.extend(text(sa_x + hw // 2, COL_Y + 117, "Certify what you have",
                  font_size=8, color="#16A34A", text_anchor="middle"))
    for i, b in enumerate(["Existing databases", "Profiled & governed", "Contract synthesized"]):
        E.extend(text(sa_x + 10, COL_Y + 132 + i * 16, f"· {b}",
                      font_size=8, color="#14532D"))

    E.extend(rect(cf_x, COL_Y + 84, hw, 108, fill="#DCFCE7", stroke="#86EFAC", rx=6))
    E.extend(text(cf_x + hw // 2, COL_Y + 102, "CONSUMER-ALIGNED",
                  font_size=9, font_weight="700", color="#14532D", text_anchor="middle"))
    E.extend(text(cf_x + hw // 2, COL_Y + 117, "Build what you need",
                  font_size=8, color="#16A34A", text_anchor="middle"))
    for i, b in enumerate(["Contract-first schema", "Agent-mapped", "Marketplace-published"]):
        E.extend(text(cf_x + 10, COL_Y + 132 + i * 16, f"· {b}",
                      font_size=8, color="#14532D"))

    # Properties
    props_y = COL_Y + 202
    props = ["Contracted", "Governed", "AI-Ready"]
    pw = (CTR_W - 32 - 2 * 6) // 3
    for i, prop in enumerate(props):
        px = CTR_X + 16 + i * (pw + 6)
        E.extend(rect(px, props_y, pw, 22, fill="#FEF3C7", stroke="#FCD34D", rx=11))
        E.extend(text(px + pw // 2, props_y + 11, prop,
                      font_size=8, font_weight="700", color="#92400E",
                      text_anchor="middle"))

    # Neo4j / PROV-O
    E.extend(rect(CTR_X + 16, COL_Y + 236, CTR_W - 32, 38,
                  fill="#F5F3FF", stroke="#DDD6FE", rx=6))
    E.extend(text(CTR_X + CTR_W // 2, COL_Y + 253,
                  "Neo4j Knowledge Graph",
                  font_size=10, font_weight="700", color="#5B21B6",
                  text_anchor="middle"))
    E.extend(text(CTR_X + CTR_W // 2, COL_Y + 268,
                  "System of record for all data assets",
                  font_size=8, color="#7C3AED", text_anchor="middle"))

    # Domain-of-data callout
    E.extend(rect(CTR_X + 16, COL_Y + 286, CTR_W - 32, 64,
                  fill="#FFF8F2", stroke="#FED7AA", rx=6))
    E.extend(text(CTR_X + CTR_W // 2, COL_Y + 304,
                  "AI applied to every domain.",
                  font_size=9, font_weight="700", color="#92400E",
                  text_anchor="middle"))
    E.extend(text(CTR_X + CTR_W // 2, COL_Y + 320,
                  "Data Workbench applies it to",
                  font_size=9, color="#78350F", text_anchor="middle"))
    E.extend(text(CTR_X + CTR_W // 2, COL_Y + 336,
                  "the domain of data.",
                  font_size=9, color="#78350F", text_anchor="middle"))

    # Stat
    E.extend(rect(CTR_X + 16, COL_Y + COL_H - 200, CTR_W - 32, 116,
                  fill="#FEF9F0", stroke="#FED7AA", rx=6))
    E.extend(text(CTR_X + CTR_W // 2, COL_Y + COL_H - 182,
                  "83%", font_size=30, font_weight="700",
                  color="#F97316", text_anchor="middle"))
    E.extend(text(CTR_X + CTR_W // 2, COL_Y + COL_H - 148,
                  "of organizations don't build",
                  font_size=8, color="#78350F", text_anchor="middle"))
    E.extend(text(CTR_X + CTR_W // 2, COL_Y + COL_H - 134,
                  "data products aligned to",
                  font_size=8, color="#78350F", text_anchor="middle"))
    E.extend(text(CTR_X + CTR_W // 2, COL_Y + COL_H - 120,
                  "business objectives.",
                  font_size=8, color="#78350F", text_anchor="middle"))
    E.extend(text(CTR_X + CTR_W // 2, COL_Y + COL_H - 100,
                  "— Gartner 2024 Survey",
                  font_size=7, color="#D97706", text_anchor="middle"))

    # Mini flow at center bottom
    flow_y = COL_Y + COL_H - 70
    flow_items  = ["Source Data", "Data Products", "AI Agents"]
    flow_fills  = ["#F1F5F9", "#DCFCE7", "#DBEAFE"]
    flow_strks  = ["#CBD5E1", "#86EFAC", "#93C5FD"]
    flow_colors = ["#475569", "#14532D", "#1E3A8A"]
    fwi = CTR_W - 32
    fi_w = (fwi - 2 * 6) // 3
    for i, (item, fill, strk, color) in enumerate(
            zip(flow_items, flow_fills, flow_strks, flow_colors)):
        fix = CTR_X + 16 + i * (fi_w + 6)
        E.extend(rect(fix, flow_y, fi_w, 28, fill=fill, stroke=strk, rx=4))
        E.extend(text(fix + fi_w // 2, flow_y + 14, item,
                      font_size=8, color=color, text_anchor="middle"))
        if i < 2:
            E.extend(text(fix + fi_w + 3, flow_y + 14, "→",
                          font_size=9, color=P["muted"], text_anchor="middle"))

    # ════════════════════════════════════════════════════════════════════════
    # RIGHT PANEL — AI as the CONSUMER
    # ════════════════════════════════════════════════════════════════════════
    E.extend(rect(RIGHT_X, COL_Y, RIGHT_W, COL_H, fill=P["ui_fill"],
                  stroke=P["ui_stroke"], stroke_width=2, rx=10))
    E.extend(header_band(RIGHT_X, COL_Y, RIGHT_W, 36,
                         label="AI THAT RUNS ON YOUR DATA",
                         fill=P["ui_stroke"], font_size=13, rx=10))
    E.extend(text(RIGHT_X + 16, COL_Y + 52,
                  "Enterprise AI needs certified, governed, semantically-rich data",
                  font_size=10, color=P["body"]))
    E.extend(text(RIGHT_X + 16, COL_Y + 67,
                  "to reason correctly and return trusted answers at scale.",
                  font_size=10, color=P["body"]))

    use_cases = [
        ("Natural Language Data Access",
         "Ask in business terms. Agent routes to the certified\n"
         "data product. Returns a governed, trusted answer."),
        ("Agentic Workflows",
         "Multi-step AI processes grounded in certified data.\n"
         "No hallucination — agents access governed sources."),
        ("Automated Analytics & Insights",
         "Agents reason directly over data products.\n"
         "Consistent KPIs, no conflicting definitions."),
        ("AI-Powered Business Applications",
         "Apps built on a foundation of certified data.\n"
         "Semantic context baked in — not retrofitted."),
    ]
    uy = COL_Y + 88
    for title, desc in use_cases:
        usecase_bullet(E, RIGHT_X + 16, uy, title, desc, accent=P["ui_stroke"])
        E.extend(divider(RIGHT_X + 18, uy + 70, RIGHT_X + RIGHT_W - 18, uy + 70,
                         color="#DBEAFE"))
        uy += 82

    # Gartner stat at bottom of right panel
    gy = COL_Y + COL_H - 92
    E.extend(rect(RIGHT_X + 14, gy, RIGHT_W - 28, 80,
                  fill="#DBEAFE", stroke="#93C5FD", rx=6))
    E.extend(text(RIGHT_X + 28, gy + 18,
                  "\"By 2027, organizations prioritizing semantics",
                  font_size=9, font_weight="600", color="#1E3A8A"))
    E.extend(text(RIGHT_X + 28, gy + 34,
                  "will achieve 80% higher AI accuracy and",
                  font_size=9, font_weight="600", color="#1E3A8A"))
    E.extend(text(RIGHT_X + 28, gy + 50,
                  "60% lower cost.\"",
                  font_size=9, font_weight="600", color="#1E3A8A"))
    E.extend(text(RIGHT_X + 28, gy + 66,
                  "Gartner  ·  May 2026  ·  ID G00852605",
                  font_size=8, color="#2563EB"))

    # ── Footer ───────────────────────────────────────────────────────────────
    E.extend(rect(0, FOOTER_Y, W, H - FOOTER_Y, fill="#0F172A", stroke="none", rx=0))
    E.extend(text(W // 2, FOOTER_Y + 32,
                  "The agents building the foundation.  "
                  "The foundation enabling the agents.",
                  font_size=17, font_weight="700", color="#F1F5F9",
                  text_anchor="middle"))
    E.extend(text(W // 2, FOOTER_Y + 60,
                  "AI innovation applied to the domain of data — "
                  "so data can power everything else.",
                  font_size=11, color="#94A3B8", text_anchor="middle"))
    E.extend(divider(W // 2 - 300, FOOTER_Y + 76,
                     W // 2 + 300, FOOTER_Y + 76, color="#1E293B"))
    E.extend(text(W // 2, FOOTER_Y + 98,
                  "What would it look like if your data engineering practice "
                  "moved at the speed of AI?",
                  font_size=11, font_weight="600", color="#64748B",
                  text_anchor="middle"))

    save(E, "arc1_ai_readiness_gap_v2.svg", width=W, height=H, bg="#F8FAFC")
    print("✓  arc1_ai_readiness_gap_v2.svg")


build()
