#!/usr/bin/env python3
"""
arc_intro_lead.py
Lead overview slide: "What is Data Workbench?"
Dark teal header · left description box · screenshot placeholders · feature cards
"""
import sys, os
sys.path.insert(0, os.path.expanduser("~/.claude/skills/svg-architect/scripts"))
from svg_lib import *

W, H = 1440, 810

TEAL_DARK   = "#0C3547"
TEAL_MID    = "#16536E"
TEAL_ACCENT = "#0EA5E9"
GREEN_ACC   = "#16A34A"
CARD_DARK   = "#1A2E3D"
INNER_DARK  = "#223344"
SS_FILL     = "#EFF6FF"
SS_STROKE   = "#BFDBFE"
RULE_COLOR  = "#CBD5E1"
BODY_DARK   = "#1E293B"
BODY_MID    = "#334155"
BODY_LIGHT  = "#64748B"
WHITE       = "#FFFFFF"
LIGHT_BG    = "#F8FAFC"


def screenshot_ph(E, x, y, w, h, label, sublabel=""):
    """Screenshot placeholder: browser-chrome mockup top + label."""
    E.extend(rect(x, y, w, h, fill=SS_FILL, stroke=SS_STROKE, stroke_width=1.5, rx=6))
    # Chrome bar
    E.extend(rect(x, y, w, 26, fill="#DBEAFE", stroke="none", rx=6))
    E.extend(rect(x, y + 20, w, 6, fill="#DBEAFE", stroke="none", rx=0))
    # Traffic-light dots
    for ci, col in enumerate(["#FCA5A5", "#FCD34D", "#86EFAC"]):
        E.extend(rect(x + 10 + ci * 16, y + 7, 11, 11, fill=col, stroke="none", rx=6))
    # URL bar
    E.extend(rect(x + 62, y + 7, w - 78, 11, fill="#BFDBFE", stroke="none", rx=3))
    # Camera/image icon in center
    mx, my = x + w // 2, y + h // 2
    E.extend(rect(mx - 26, my - 22, 52, 34, fill="#BFDBFE", stroke="#93C5FD", rx=5))
    E.extend(rect(mx - 10, my - 14, 20, 18, fill="#60A5FA", stroke="none", rx=9))
    # Label text
    E.extend(text(mx, my + 24, label,
                  font_size=10, font_weight="600", color=BODY_MID, text_anchor="middle"))
    if sublabel:
        E.extend(text(mx, my + 40, sublabel,
                      font_size=8, color=BODY_LIGHT, text_anchor="middle"))


def feat_card(E, x, y, w, h, title, body_lines):
    E.extend(rect(x, y, w, h, fill=WHITE, stroke=RULE_COLOR, stroke_width=1.5, rx=6))
    E.extend(rect(x, y, w, 4, fill=TEAL_ACCENT, stroke="none", rx=0))
    E.extend(text(x + 14, y + 22, title,
                  font_size=10, font_weight="700", color=TEAL_DARK))
    for i, ln in enumerate(body_lines):
        E.extend(text(x + 14, y + 40 + i * 15, ln,
                      font_size=9, color=BODY_MID))


def build():
    E = []

    # ── HEADER ────────────────────────────────────────────────────────────────
    E.extend(rect(0, 0, W, 92, fill=TEAL_DARK, stroke="none", rx=0))
    E.extend(rect(0, 0, 640, 92, fill=TEAL_MID, stroke="none", rx=0))
    E.extend(rect(0, 89, W, 3, fill=TEAL_ACCENT, stroke="none", rx=0))

    E.extend(text(46, 38, "Data Workbench",
                  font_size=30, font_weight="700", color=WHITE))
    E.extend(text(46, 66,
                  "An AI-powered platform for building, governing, and publishing enterprise data products",
                  font_size=11, color="#94A3B8"))

    E.extend(text(W - 46, 36,
                  "AI-Powered  ·  Human-Governed  ·  AI-Ready",
                  font_size=11, font_weight="600", color=TEAL_ACCENT, text_anchor="end"))
    E.extend(text(W - 46, 60,
                  "Engineering Workbench  +  Product Workbench",
                  font_size=10, color="#64748B", text_anchor="end"))

    # ── LEFT DESCRIPTION BOX ──────────────────────────────────────────────────
    LB_X, LB_Y, LB_W, LB_H = 40, 100, 330, 412

    E.extend(rect(LB_X, LB_Y, LB_W, LB_H, fill=CARD_DARK, stroke="none", rx=8))
    E.extend(rect(LB_X, LB_Y, LB_W, 4, fill=TEAL_ACCENT, stroke="none", rx=0))

    E.extend(text(LB_X + 20, LB_Y + 30, "What is Data Workbench?",
                  font_size=13, font_weight="700", color=WHITE))

    desc = [
        "A workbench where AI agents and humans",
        "collaborate to build governed data products.",
        "Agent skills automate the heavy lifting —",
        "engineers and product owners review and",
        "approve at every step.",
    ]
    for i, ln in enumerate(desc):
        E.extend(text(LB_X + 20, LB_Y + 56 + i * 17, ln,
                      font_size=10, color="#CBD5E1"))

    # Divider
    E.extend(divider(LB_X + 16, LB_Y + 150, LB_X + LB_W - 16, LB_Y + 150,
                     color="#2A4A5E", stroke_width=1))

    # Two shells
    E.extend(text(LB_X + 20, LB_Y + 168, "Two Workbench Shells",
                  font_size=11, font_weight="700", color=TEAL_ACCENT))

    shells = [
        ("Engineering Workbench",
         "AI pipeline: discover → profile →",
         "map → quality → publish"),
        ("Product Workbench",
         "Design products contract-first,",
         "validate and publish to marketplace"),
    ]
    for i, (title, l1, l2) in enumerate(shells):
        sy = LB_Y + 190 + i * 68
        E.extend(rect(LB_X + 14, sy, LB_W - 28, 58,
                      fill=INNER_DARK, stroke="#2A4A5E", stroke_width=1, rx=6))
        E.extend(text(LB_X + 28, sy + 18, title,
                      font_size=10, font_weight="700", color=WHITE))
        E.extend(text(LB_X + 28, sy + 34, l1, font_size=9, color="#94A3B8"))
        E.extend(text(LB_X + 28, sy + 49, l2, font_size=9, color="#94A3B8"))

    # Divider
    E.extend(divider(LB_X + 16, LB_Y + 332, LB_X + LB_W - 16, LB_Y + 332,
                     color="#2A4A5E", stroke_width=1))

    # Knowledge graph note
    E.extend(text(LB_X + 20, LB_Y + 352, "Knowledge Graph",
                  font_size=11, font_weight="700", color=TEAL_ACCENT))
    E.extend(text(LB_X + 20, LB_Y + 370, "Neo4j: all assets, semantics,",
                  font_size=9, color="#94A3B8"))
    E.extend(text(LB_X + 20, LB_Y + 386, "lineage & provenance in one graph",
                  font_size=9, color="#94A3B8"))

    # ── SCREENSHOT PLACEHOLDERS ───────────────────────────────────────────────
    SS_LEFT = LB_X + LB_W + 12          # 382
    SS_AVAIL_W = W - SS_LEFT - 40       # 1018
    SS_TOP_H = 198
    SS_GAP_V = 10
    SS_BOT_H = LB_H - SS_TOP_H - SS_GAP_V   # 204
    SS_HALF = (SS_AVAIL_W - 12) // 2         # 503

    screenshot_ph(
        E, SS_LEFT, LB_Y, SS_HALF, SS_TOP_H,
        "Engineering Workbench — Pipeline & Stages",
        "Suggest: project list with pipeline stage progress chips"
    )
    screenshot_ph(
        E, SS_LEFT + SS_HALF + 12, LB_Y, SS_AVAIL_W - SS_HALF - 12, SS_TOP_H,
        "Knowledge Graph Explorer",
        "Suggest: Neo4j browser graph visualization"
    )
    screenshot_ph(
        E, SS_LEFT, LB_Y + SS_TOP_H + SS_GAP_V, SS_AVAIL_W, SS_BOT_H,
        "Product Workbench — Marketplace & Product Authoring",
        "Suggest: product wizard OR published marketplace product cards"
    )

    # ── KEY FEATURES ──────────────────────────────────────────────────────────
    FEAT_LABEL_Y = LB_Y + LB_H + 14     # 526
    FEAT_TOP_Y   = FEAT_LABEL_Y + 22    # 548
    FEAT_H       = H - FEAT_TOP_Y - 14  # 248

    n = 5
    FEAT_GAP = 12
    FEAT_W = (W - 80 - (n - 1) * FEAT_GAP) // n  # 262

    E.extend(text(40, FEAT_LABEL_Y + 2, "Key Features",
                  font_size=13, font_weight="700", color=TEAL_DARK))
    E.extend(rect(40, FEAT_LABEL_Y + 10, 108, 3, fill=TEAL_ACCENT, stroke="none", rx=1))

    features = [
        ("Agent-Powered Pipeline",
         ["20+ AI agent skills built for", "data engineering — discovery,",
          "profiling, mapping, quality,"]),
        ("Human-in-the-Loop",
         ["Every AI output reviewed and", "approved before it persists.",
          "Provenance tracked at each step."]),
        ("Data Contracts (ODCS)",
         ["Every product carries a machine-", "readable contract: schema, rules,",
          "lineage, and governance built in."]),
        ("Two Product Types",
         ["Source-aligned: certify existing", "databases. Consumer-aligned:",
          "design for your AI use cases."]),
        ("Marketplace & Discoverability",
         ["Published products searchable", "and consumable by AI agents",
          "and teams across the enterprise."]),
    ]

    for i, (title, lines) in enumerate(features):
        fx = 40 + i * (FEAT_W + FEAT_GAP)
        feat_card(E, fx, FEAT_TOP_Y, FEAT_W, FEAT_H, title, lines)

    save(E, "arc_intro_lead.svg", width=W, height=H, bg=LIGHT_BG)
    print("✓  arc_intro_lead.svg")


build()
