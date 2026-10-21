#!/usr/bin/env python3
"""
arc_features_detail.py
Detail slide: "How Data Workbench Works"
Three-phase lifecycle flow + screenshot placeholder + value outcome cards
"""
import sys, os
sys.path.insert(0, os.path.expanduser("~/.claude/skills/svg-architect/scripts"))
from svg_lib import *

W, H = 1440, 810

TEAL_DARK   = "#0C3547"
TEAL_MID    = "#16536E"
TEAL_ACCENT = "#0EA5E9"
GREEN_DARK  = "#14532D"
GREEN_MID   = "#16A34A"
GREEN_FILL  = "#DCFCE7"
GREEN_STROKE= "#86EFAC"
BLUE_DARK   = "#1E3A8A"
BLUE_MID    = "#2563EB"
BLUE_FILL   = "#DBEAFE"
BLUE_STROKE = "#93C5FD"
PURPLE_DARK = "#4C1D95"
PURPLE_MID  = "#7C3AED"
PURPLE_FILL = "#EDE9FE"
PURPLE_STR  = "#C4B5FD"
RULE_COLOR  = "#CBD5E1"
BODY_DARK   = "#1E293B"
BODY_MID    = "#334155"
BODY_LIGHT  = "#64748B"
WHITE       = "#FFFFFF"
LIGHT_BG    = "#F8FAFC"


def phase_panel(E, x, y, w, h, num, title, subtitle,
                fill, stroke, head_fill, dark, agent_skills, human_role):
    """One phase panel in the lifecycle flow."""
    E.extend(rect(x, y, w, h, fill=fill, stroke=stroke, stroke_width=2, rx=8))
    # Header band
    E.extend(rect(x, y, w, 44, fill=head_fill, stroke="none", rx=8))
    E.extend(rect(x, y + 38, w, 6, fill=head_fill, stroke="none", rx=0))
    # Phase badge
    E.extend(rect(x + 12, y + 10, 52, 22, fill=WHITE, stroke="none", rx=11))
    E.extend(text(x + 38, y + 22, f"Phase {num}",
                  font_size=9, font_weight="700", color=head_fill, text_anchor="middle"))
    E.extend(text(x + 76, y + 23, title,
                  font_size=13, font_weight="700", color=WHITE))
    E.extend(text(x + 16, y + 56, subtitle,
                  font_size=10, color=dark))

    # Divider
    E.extend(divider(x + 14, y + 72, x + w - 14, y + 72, color=stroke, stroke_width=1))

    # Agent skills
    E.extend(text(x + 16, y + 90, "Agent Skills",
                  font_size=10, font_weight="700", color=head_fill))
    for i, skill in enumerate(agent_skills):
        sy = y + 108 + i * 22
        E.extend(rect(x + 14, sy - 2, w - 28, 18,
                      fill=WHITE, stroke="none", rx=3))
        E.extend(rect(x + 14, sy - 2, 3, 18, fill=head_fill, stroke="none", rx=1))
        E.extend(text(x + 24, sy + 10, skill, font_size=9, color=BODY_DARK))

    # Human role
    hr_y = y + h - 64
    E.extend(divider(x + 14, hr_y, x + w - 14, hr_y, color=stroke, stroke_width=1))
    E.extend(text(x + 16, hr_y + 16, "Human Role",
                  font_size=10, font_weight="700", color=head_fill))
    E.extend(text(x + 16, hr_y + 32, human_role[0], font_size=9, color=dark))
    if len(human_role) > 1:
        E.extend(text(x + 16, hr_y + 46, human_role[1], font_size=9, color=dark))


def screenshot_ph(E, x, y, w, h, label, sublabel=""):
    E.extend(rect(x, y, w, h, fill="#EFF6FF", stroke="#BFDBFE", stroke_width=1.5, rx=6))
    E.extend(rect(x, y, w, 26, fill="#DBEAFE", stroke="none", rx=6))
    E.extend(rect(x, y + 20, w, 6, fill="#DBEAFE", stroke="none", rx=0))
    for ci, col in enumerate(["#FCA5A5", "#FCD34D", "#86EFAC"]):
        E.extend(rect(x + 10 + ci * 16, y + 7, 11, 11, fill=col, stroke="none", rx=6))
    E.extend(rect(x + 62, y + 7, w - 78, 11, fill="#BFDBFE", stroke="none", rx=3))
    mx, my = x + w // 2, y + h // 2
    E.extend(rect(mx - 26, my - 22, 52, 34, fill="#BFDBFE", stroke="#93C5FD", rx=5))
    E.extend(rect(mx - 10, my - 14, 20, 18, fill="#60A5FA", stroke="none", rx=9))
    E.extend(text(mx, my + 24, label,
                  font_size=10, font_weight="600", color=BODY_MID, text_anchor="middle"))
    if sublabel:
        E.extend(text(mx, my + 40, sublabel,
                      font_size=8, color=BODY_LIGHT, text_anchor="middle"))


def outcome_card(E, x, y, w, h, title, lines, accent):
    E.extend(rect(x, y, w, h, fill=WHITE, stroke=RULE_COLOR, stroke_width=1.5, rx=6))
    E.extend(rect(x, y, w, 4, fill=accent, stroke="none", rx=0))
    E.extend(text(x + 14, y + 22, title,
                  font_size=10, font_weight="700", color=TEAL_DARK))
    for i, ln in enumerate(lines):
        E.extend(text(x + 14, y + 40 + i * 15, ln, font_size=9, color=BODY_MID))


def build():
    E = []

    # ── HEADER ────────────────────────────────────────────────────────────────
    E.extend(rect(0, 0, W, 92, fill=TEAL_DARK, stroke="none", rx=0))
    E.extend(rect(0, 0, 640, 92, fill=TEAL_MID, stroke="none", rx=0))
    E.extend(rect(0, 89, W, 3, fill=TEAL_ACCENT, stroke="none", rx=0))

    E.extend(text(46, 38, "How Data Workbench Works",
                  font_size=26, font_weight="700", color=WHITE))
    E.extend(text(46, 66,
                  "Three lifecycle phases — AI does the heavy lifting, humans provide judgment and approval at every stage",
                  font_size=11, color="#94A3B8"))

    E.extend(text(W - 46, 36, "From raw database to AI-ready data product",
                  font_size=11, font_weight="600", color=TEAL_ACCENT, text_anchor="end"))
    E.extend(text(W - 46, 60, "Governed  ·  Contracted  ·  Discoverable",
                  font_size=10, color="#64748B", text_anchor="end"))

    # ── THREE-PHASE LIFECYCLE ─────────────────────────────────────────────────
    PHASE_Y = 100
    PHASE_H = 376
    PHASE_GAP = 14
    PHASE_W = (W - 80 - 2 * PHASE_GAP) // 3   # 434

    # Phase 1: Discover & Profile
    phase_panel(
        E, 40, PHASE_Y, PHASE_W, PHASE_H,
        num=1, title="Discover & Profile",
        subtitle="AI agents scan your source databases, build the knowledge graph,",
        fill="#F0FFF4", stroke=GREEN_STROKE,
        head_fill=GREEN_MID, dark=GREEN_DARK,
        agent_skills=[
            "data-discovery — schema, columns, FKs",
            "data-profiling — row counts, patterns, types",
            "metadata-enrichment — AI-written descriptions",
            "column-name-standardization — naming rules",
            "data-quality-rule-generation — observation rules",
        ],
        human_role=["Data Engineer: review AI-generated descriptions",
                    "and approve or edit recommended names"]
    )

    # Arrow 1→2
    ax1 = 40 + PHASE_W
    ay = PHASE_Y + PHASE_H // 2
    E.extend(arrow(ax1 + 2, ay, ax1 + PHASE_GAP - 2, ay,
                   color=TEAL_ACCENT, stroke_width=2.5))

    # Phase 2: Design & Contract
    phase_panel(
        E, 40 + PHASE_W + PHASE_GAP, PHASE_Y, PHASE_W, PHASE_H,
        num=2, title="Design & Contract",
        subtitle="Product Owner designs the data product; ODCS contract is authored",
        fill=BLUE_FILL, stroke=BLUE_STROKE,
        head_fill=BLUE_MID, dark=BLUE_DARK,
        agent_skills=[
            "odcs-specification — contract authoring wizard",
            "domain-rule-enhancement — catalog-based rules",
            "data-mapping-neo4j — source-to-target mapping",
            "source-product-validation — PO approval gate",
            "data-product-schema-advisor — column guidance",
        ],
        human_role=["Product Owner: validate names, descriptions,",
                    "schema and approve observation rules"]
    )

    # Arrow 2→3
    ax2 = 40 + PHASE_W + PHASE_GAP + PHASE_W
    E.extend(arrow(ax2 + 2, ay, ax2 + PHASE_GAP - 2, ay,
                   color=TEAL_ACCENT, stroke_width=2.5))

    # Phase 3: Serve & Publish
    phase_panel(
        E, 40 + 2 * (PHASE_W + PHASE_GAP), PHASE_Y, PHASE_W, PHASE_H,
        num=3, title="Serve & Publish",
        subtitle="Virtual serving views generated; product published to marketplace",
        fill=PURPLE_FILL, stroke=PURPLE_STR,
        head_fill=PURPLE_MID, dark=PURPLE_DARK,
        agent_skills=[
            "odcs-to-dprod — materialise product graph nodes",
            "data-serving-virtual-view — SQL view generation",
            "data-quality-testing — test suite execution",
            "data-product-scoring — OSI readiness score",
            "marketplace-publish — discovery + AI consumption",
        ],
        human_role=["Data Engineer: review serving views,",
                    "confirm deployment to marketplace"]
    )

    # ── SCREENSHOT PLACEHOLDER (optional accent) ──────────────────────────────
    # Thin screenshot strip below phases showing mapping/review in action
    SS_Y = PHASE_Y + PHASE_H + 10
    SS_H = 80
    screenshot_ph(
        E, 40, SS_Y, W - 80, SS_H,
        "Suggest: Mapping Review Panel — AI column mappings awaiting engineer approval",
        "Shows human-in-the-loop review: source → target column mappings with transform logic and quality rating"
    )

    # ── VALUE OUTCOMES ────────────────────────────────────────────────────────
    OUT_LABEL_Y = SS_Y + SS_H + 12
    OUT_TOP_Y   = OUT_LABEL_Y + 22
    OUT_H       = H - OUT_TOP_Y - 14

    E.extend(text(40, OUT_LABEL_Y + 2, "What You Get",
                  font_size=13, font_weight="700", color=TEAL_DARK))
    E.extend(rect(40, OUT_LABEL_Y + 10, 98, 3, fill=TEAL_ACCENT, stroke="none", rx=1))

    n = 4
    OUT_GAP = 14
    OUT_W = (W - 80 - (n - 1) * OUT_GAP) // n   # 337

    outcomes = [
        ("Governed Data Products",
         ["Every product carries an ODCS contract",
          "with schema, quality rules, and lineage.", "Governance is built in, not bolted on."],
         TEAL_ACCENT),
        ("AI-Ready Foundation",
         ["Certified products that AI applications",
          "can trust and reason over reliably.", "Semantic layer connects data to concepts."],
         GREEN_MID),
        ("Full Lineage & Provenance",
         ["Every transformation, approval, and edit",
          "captured in the Neo4j knowledge graph.", "Know where every data field came from."],
         BLUE_MID),
        ("Marketplace Discoverability",
         ["Products published and searchable across",
          "teams and AI agents in the enterprise.", "Source and consumer products cross-linked."],
         PURPLE_MID),
    ]

    for i, (title, lines, accent) in enumerate(outcomes):
        ox = 40 + i * (OUT_W + OUT_GAP)
        outcome_card(E, ox, OUT_TOP_Y, OUT_W, OUT_H, title, lines, accent)

    save(E, "arc_features_detail.svg", width=W, height=H, bg=LIGHT_BG)
    print("✓  arc_features_detail.svg")


build()
