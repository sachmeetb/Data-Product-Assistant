#!/usr/bin/env python3
"""
arc2_months_to_days.py — v2
"Every AI use case starts with a data product you haven't built yet."
Before/after pipeline: database approach vs. data product approach.
"""
import sys, os
sys.path.insert(0, os.path.expanduser("~/.claude/skills/svg-architect/scripts"))
from svg_lib import *

W, H = 1440, 810

# Context strip below header
CTX_Y = 78; CTX_H = 36

# Pipeline layout
STAGE_XS  = [40, 296, 552, 808, 1064]
STAGE_W   = 216
BEFORE_Y  = 140; BEFORE_H  = 175
AFTER_Y   = 398; AFTER_H   = 190

# Result box
RESULT_X = 1320; RESULT_W = 100

# Divider between before/after
DIV_Y = 350

# Stats row
STATS_Y = 622
FOOTER_Y = 748

# Colors: before = "database approach"
DB_FILL   = "#FFF7ED"; DB_STROKE = "#FCA5A5"
DB_HEAD   = "#DC2626"; DB_BODY   = "#7F1D1D"; DB_TIME   = "#EF4444"

# Colors: after = "data product approach"
DP_FILL   = "#F0FFF4"; DP_STROKE = "#86EFAC"
DP_HEAD   = "#16A34A"; DP_BODY   = "#14532D"; DP_TIME   = "#16A34A"


def stage_before(E, xi, title, time_label, body_lines):
    E.extend(rect(xi, BEFORE_Y, STAGE_W, BEFORE_H,
                  fill=DB_FILL, stroke=DB_STROKE, stroke_width=1.5, rx=8))
    E.extend(rect(xi, BEFORE_Y, STAGE_W, 28, fill=DB_HEAD, stroke="none", rx=8))
    E.extend(text(xi + STAGE_W // 2, BEFORE_Y + 14, title,
                  font_size=10, font_weight="700", color="#FFFFFF", text_anchor="middle"))
    E.extend(rect(xi + STAGE_W // 2 - 46, BEFORE_Y + 36, 92, 20,
                  fill="#FEE2E2", stroke=DB_STROKE, rx=10))
    E.extend(text(xi + STAGE_W // 2, BEFORE_Y + 46, time_label,
                  font_size=9, font_weight="700", color=DB_TIME, text_anchor="middle"))
    for i, ln in enumerate(body_lines):
        E.extend(text(xi + 12, BEFORE_Y + 68 + i * 16, ln,
                      font_size=10, color=DB_BODY))


def stage_after(E, xi, title, time_label, skill_name, body_lines):
    E.extend(rect(xi, AFTER_Y, STAGE_W, AFTER_H,
                  fill=DP_FILL, stroke=DP_STROKE, stroke_width=2, rx=8))
    E.extend(rect(xi, AFTER_Y, STAGE_W, 28, fill=DP_HEAD, stroke="none", rx=8))
    E.extend(text(xi + STAGE_W // 2, AFTER_Y + 14, title,
                  font_size=10, font_weight="700", color="#FFFFFF", text_anchor="middle"))
    E.extend(rect(xi + STAGE_W // 2 - 32, AFTER_Y + 36, 64, 20,
                  fill="#DCFCE7", stroke=DP_STROKE, rx=10))
    E.extend(text(xi + STAGE_W // 2, AFTER_Y + 46, time_label,
                  font_size=9, font_weight="700", color=DP_TIME, text_anchor="middle"))
    E.extend(rect(xi + 10, AFTER_Y + AFTER_H - 30, STAGE_W - 20, 18,
                  fill="#D1FAE5", stroke="#6EE7B7", rx=9))
    E.extend(text(xi + STAGE_W // 2, AFTER_Y + AFTER_H - 21, skill_name,
                  font_size=7, font_weight="600", color="#065F46", text_anchor="middle"))
    for i, ln in enumerate(body_lines):
        E.extend(text(xi + 12, AFTER_Y + 68 + i * 15, ln,
                      font_size=9, color=DP_BODY))


def build():
    E = []

    # ── Header ──────────────────────────────────────────────────────────────
    E.extend(rect(0, 0, W, CTX_Y, fill="#0F172A", stroke="none", rx=0))
    E.extend(text(48, 28,
                  "Every AI use case starts with a data product you haven't built yet.",
                  font_size=21, font_weight="700", color="#FFFFFF"))
    E.extend(text(48, 57,
                  "Agent skills automate the heavy lifting. "
                  "Engineers focus on judgment, quality, and business value.",
                  font_size=10, color="#94A3B8"))
    E.extend(rect(48, 74, 120, 3, fill=P["skills_stroke"], stroke="none", rx=1))

    # ── Context strip ────────────────────────────────────────────────────────
    E.extend(rect(0, CTX_Y, W, CTX_H, fill="#FFF8F2", stroke="none", rx=0))
    E.extend(text(W // 2, CTX_Y + 18,
                  "Databases are not data products.  "
                  "Turning one into the other is where AI initiatives stall — "
                  "and where Data Workbench starts.",
                  font_size=10, font_weight="600", color="#92400E",
                  text_anchor="middle"))

    # ── BEFORE row label ─────────────────────────────────────────────────────
    E.extend(text(LEFT_X := 48, 128,
                  "THE DATABASE APPROACH  —  undocumented, ungoverned, no contract, no lineage",
                  font_size=9, font_weight="700", color=DB_TIME))

    # ── Before stages ────────────────────────────────────────────────────────
    stage_before(E, STAGE_XS[0], "Schema Discovery",
                 "3–5 weeks",
                 ["DBAs manually document", "tables, columns, types,", "relationships"])
    stage_before(E, STAGE_XS[1], "Data Documentation",
                 "2–4 weeks",
                 ["Analysts write descriptions", "— often incomplete", "or out of date"])
    stage_before(E, STAGE_XS[2], "Column Mapping",
                 "2–3 weeks",
                 ["Engineers map source", "to target in spreadsheets", "— no lineage"])
    stage_before(E, STAGE_XS[3], "Quality Rules",
                 "1–3 weeks",
                 ["Rules defined ad hoc,", "per-project, never", "reused or governed"])
    stage_before(E, STAGE_XS[4], "Deploy & Document",
                 "1–2 weeks",
                 ["View built manually.", "No contract, no", "marketplace entry"])

    # Arrows between before stages
    for i in range(4):
        mid_y = BEFORE_Y + BEFORE_H // 2
        E.extend(arrow(STAGE_XS[i] + STAGE_W, mid_y,
                       STAGE_XS[i + 1], mid_y,
                       color=DB_TIME, stroke_width=1.5))

    # Result: ungoverned database table
    resy = BEFORE_Y + 22
    E.extend(rect(RESULT_X, resy, RESULT_W, 110,
                  fill="#FEE2E2", stroke="#FCA5A5", stroke_width=1.5, rx=8))
    E.extend(text(RESULT_X + RESULT_W // 2, resy + 20, "Ungoverned",
                  font_size=9, font_weight="700", color=DB_TIME, text_anchor="middle"))
    E.extend(text(RESULT_X + RESULT_W // 2, resy + 38, "database table",
                  font_size=9, color=DB_BODY, text_anchor="middle"))
    E.extend(text(RESULT_X + RESULT_W // 2, resy + 68, "8–17",
                  font_size=20, font_weight="700", color=DB_TIME, text_anchor="middle"))
    E.extend(text(RESULT_X + RESULT_W // 2, resy + 90, "weeks",
                  font_size=9, color=DB_BODY, text_anchor="middle"))
    E.extend(arrow(STAGE_XS[4] + STAGE_W, BEFORE_Y + BEFORE_H // 2,
                   RESULT_X, BEFORE_Y + BEFORE_H // 2,
                   color=DB_TIME, stroke_width=1.5))

    # ── Divider ──────────────────────────────────────────────────────────────
    E.extend(divider(40, DIV_Y, W - 40, DIV_Y, color="#CBD5E1", stroke_width=1.5))
    E.extend(rect(620, DIV_Y - 12, 200, 24, fill="#F8FAFC", stroke="#CBD5E1", rx=4))
    E.extend(text(720, DIV_Y, "vs.", font_size=11, font_weight="700",
                  color=P["muted"], text_anchor="middle"))

    # ── AFTER row label ──────────────────────────────────────────────────────
    E.extend(text(48, AFTER_Y - 12,
                  "THE DATA PRODUCT APPROACH  —  contracted, governed, documented, AI-consumable",
                  font_size=9, font_weight="700", color=DP_TIME))

    # ── After stages ─────────────────────────────────────────────────────────
    stage_after(E, STAGE_XS[0], "Auto-Discovery",
                "2–4 hrs", "data-discovery",
                ["Agents scan every table,", "column, and FK —", "fully documented to graph"])
    stage_after(E, STAGE_XS[1], "Profile & Enrich",
                "1–3 hrs", "data-profiling + metadata-enrichment",
                ["AI writes descriptions", "from profiling data.", "Human approves or edits."])
    stage_after(E, STAGE_XS[2], "Intelligent Mapping",
                "1–2 hrs", "data-mapping-neo4j",
                ["AI maps source to target", "with full lineage.", "Human reviews, approves."])
    stage_after(E, STAGE_XS[3], "Quality & Rules",
                "1–2 hrs", "data-quality-rule-generation",
                ["Domain rules from catalog.", "Observation rules from", "profiling. Auto-generated."])
    stage_after(E, STAGE_XS[4], "Serve & Publish",
                "< 1 hr", "data-serving-virtual-view",
                ["Virtual view generated.", "ODCS contract attached.", "Published to marketplace."])

    # Arrows between after stages
    for i in range(4):
        mid_y = AFTER_Y + AFTER_H // 2
        E.extend(arrow(STAGE_XS[i] + STAGE_W, mid_y,
                       STAGE_XS[i + 1], mid_y,
                       color=DP_TIME, stroke_width=2))

    # Result: governed data product
    resy2 = AFTER_Y + 4
    E.extend(rect(RESULT_X, resy2, RESULT_W, 168,
                  fill="#DCFCE7", stroke=DP_STROKE, stroke_width=2, rx=8))
    E.extend(text(RESULT_X + RESULT_W // 2, resy2 + 20, "Governed",
                  font_size=9, font_weight="700", color=DP_TIME, text_anchor="middle"))
    E.extend(text(RESULT_X + RESULT_W // 2, resy2 + 38, "Data Product",
                  font_size=9, color=DP_BODY, text_anchor="middle"))
    E.extend(text(RESULT_X + RESULT_W // 2, resy2 + 72, "Days",
                  font_size=20, font_weight="700", color=DP_TIME, text_anchor="middle"))
    checks = ["✓ Contract", "✓ Lineage", "✓ Quality Rules", "✓ Marketplace"]
    for i, ck in enumerate(checks):
        E.extend(text(RESULT_X + RESULT_W // 2, resy2 + 100 + i * 16, ck,
                      font_size=8, color=DP_BODY, text_anchor="middle"))
    E.extend(arrow(STAGE_XS[4] + STAGE_W, AFTER_Y + AFTER_H // 2,
                   RESULT_X, AFTER_Y + AFTER_H // 2,
                   color=DP_TIME, stroke_width=2))

    # ── Stats row ────────────────────────────────────────────────────────────
    stat_items = [
        ("20+ Agent Skills",
         "Each with domain expertise\nand provenance-tracked approvals"),
        ("Two Product Types",
         "Source-aligned: certify what you have.\nConsumer-aligned: build what you need."),
        ("Human-in-the-Loop",
         "Engineers approve AI suggestions.\nEvery decision improves future runs."),
        ("Active Learning",
         "Rejections and edits train the system.\nAccuracy improves with every project."),
    ]
    sw = (W - 80 - 3 * 16) // 4
    for i, (title, body) in enumerate(stat_items):
        sx = 40 + i * (sw + 16)
        E.extend(rect(sx, STATS_Y, sw, 102,
                      fill="#F1F5F9", stroke="#CBD5E1", stroke_width=1, rx=6))
        E.extend(text(sx + 14, STATS_Y + 22, title,
                      font_size=12, font_weight="700", color=P["title"]))
        E.extend(text(sx + 14, STATS_Y + 46, body,
                      font_size=10, color=P["body"]))

    # ── Footer ───────────────────────────────────────────────────────────────
    E.extend(rect(0, FOOTER_Y, W, H - FOOTER_Y, fill="#1E293B", stroke="none", rx=0))
    E.extend(text(W // 2, FOOTER_Y + 24,
                  "From source database to governed, marketplace-ready data product — "
                  "measured in days, not months.",
                  font_size=13, font_weight="700", color="#F1F5F9",
                  text_anchor="middle"))
    E.extend(text(W // 2, FOOTER_Y + 50,
                  "With full provenance, lineage, and human approval at every step.",
                  font_size=10, color="#64748B", text_anchor="middle"))
    E.extend(divider(W // 2 - 280, FOOTER_Y + 64,
                     W // 2 + 280, FOOTER_Y + 64, color="#334155"))
    E.extend(text(W // 2, FOOTER_Y + 84,
                  "How many AI-ready data products does your organization have today?",
                  font_size=11, font_weight="600", color="#94A3B8",
                  text_anchor="middle"))

    save(E, "arc2_months_to_days.svg", width=W, height=H, bg="#F8FAFC")
    print("✓  arc2_months_to_days.svg")


build()
