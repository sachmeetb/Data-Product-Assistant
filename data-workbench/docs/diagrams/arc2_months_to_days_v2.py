#!/usr/bin/env python3
"""
arc2_months_to_days_v2.py
"AI agents doing the data work, so AI agents can do the business work."
Pipeline: manual approach vs. agent-powered → What enterprise AI can now do.
"""
import sys, os
sys.path.insert(0, os.path.expanduser("~/.claude/skills/svg-architect/scripts"))
from svg_lib import *

W, H = 1440, 810

# Pipeline geometry (slightly compressed vs v1 to make room for enables row)
STAGE_XS = [40, 296, 552, 808, 1064]
STAGE_W  = 216
BEFORE_Y = 138; BEFORE_H = 155
AFTER_Y  = 348; AFTER_H  = 170

# Result boxes
RESULT_X = 1320; RESULT_W = 100

# Divider between before/after
DIV_Y = 322

# "Enables" row
ENABLES_Y = 548; ENABLES_H = 80

# Stats + footer
STATS_Y  = 650; STATS_H = 82
FOOTER_Y = 744

# Before (database approach)
DB_FILL   = "#FFF7ED"; DB_STROKE = "#FCA5A5"
DB_HEAD   = "#DC2626"; DB_BODY   = "#7F1D1D"; DB_TIME   = "#EF4444"

# After (data product approach)
DP_FILL   = "#F0FFF4"; DP_STROKE = "#86EFAC"
DP_HEAD   = "#16A34A"; DP_BODY   = "#14532D"; DP_TIME   = "#16A34A"

# Enables (AI consumer)
EN_FILL   = "#EFF6FF"; EN_STROKE = "#93C5FD"
EN_HEAD   = "#2563EB"; EN_BODY   = "#1E3A8A"


def stage_before(E, xi, title, time_label, body_lines):
    E.extend(rect(xi, BEFORE_Y, STAGE_W, BEFORE_H,
                  fill=DB_FILL, stroke=DB_STROKE, stroke_width=1.5, rx=8))
    E.extend(rect(xi, BEFORE_Y, STAGE_W, 26, fill=DB_HEAD, stroke="none", rx=8))
    E.extend(text(xi + STAGE_W // 2, BEFORE_Y + 13, title,
                  font_size=10, font_weight="700", color="#FFFFFF", text_anchor="middle"))
    E.extend(rect(xi + STAGE_W // 2 - 44, BEFORE_Y + 33, 88, 18,
                  fill="#FEE2E2", stroke=DB_STROKE, rx=9))
    E.extend(text(xi + STAGE_W // 2, BEFORE_Y + 42, time_label,
                  font_size=9, font_weight="700", color=DB_TIME, text_anchor="middle"))
    for i, ln in enumerate(body_lines):
        E.extend(text(xi + 10, BEFORE_Y + 62 + i * 16, ln,
                      font_size=9, color=DB_BODY))


def stage_after(E, xi, title, time_label, skill_name, body_lines):
    E.extend(rect(xi, AFTER_Y, STAGE_W, AFTER_H,
                  fill=DP_FILL, stroke=DP_STROKE, stroke_width=2, rx=8))
    E.extend(rect(xi, AFTER_Y, STAGE_W, 26, fill=DP_HEAD, stroke="none", rx=8))
    E.extend(text(xi + STAGE_W // 2, AFTER_Y + 13, title,
                  font_size=10, font_weight="700", color="#FFFFFF", text_anchor="middle"))
    E.extend(rect(xi + STAGE_W // 2 - 30, AFTER_Y + 33, 60, 18,
                  fill="#DCFCE7", stroke=DP_STROKE, rx=9))
    E.extend(text(xi + STAGE_W // 2, AFTER_Y + 42, time_label,
                  font_size=9, font_weight="700", color=DP_TIME, text_anchor="middle"))
    E.extend(rect(xi + 8, AFTER_Y + AFTER_H - 28, STAGE_W - 16, 18,
                  fill="#D1FAE5", stroke="#6EE7B7", rx=9))
    E.extend(text(xi + STAGE_W // 2, AFTER_Y + AFTER_H - 19, skill_name,
                  font_size=7, font_weight="600", color="#065F46", text_anchor="middle"))
    for i, ln in enumerate(body_lines):
        E.extend(text(xi + 10, AFTER_Y + 62 + i * 14, ln,
                      font_size=9, color=DP_BODY))


def build():
    E = []

    # ── Header ──────────────────────────────────────────────────────────────
    E.extend(rect(0, 0, W, 78, fill="#0F172A", stroke="none", rx=0))
    E.extend(text(48, 26,
                  "AI agents doing the data work.  "
                  "So AI agents can do the business work.",
                  font_size=21, font_weight="700", color="#FFFFFF"))
    E.extend(text(48, 56,
                  "We apply the same AI agent technology to data engineering "
                  "that you'll use to run your business — building the foundation first.",
                  font_size=10, color="#94A3B8"))
    E.extend(rect(48, 74, 120, 3, fill=P["skills_stroke"], stroke="none", rx=1))

    # ── Context strip ────────────────────────────────────────────────────────
    E.extend(rect(0, 78, W, 34, fill="#FFF8F2", stroke="none", rx=0))
    E.extend(text(W // 2, 95,
                  "AI can be applied to any domain.  "
                  "Data Workbench applies it to the domain of data engineering — "
                  "building the foundation AI needs to work.",
                  font_size=10, font_weight="600", color="#92400E",
                  text_anchor="middle"))

    # ── BEFORE row ───────────────────────────────────────────────────────────
    E.extend(text(48, 126, "WITHOUT DATA WORKBENCH  —  manual, fragmented, ungoverned",
                  font_size=9, font_weight="700", color=DB_TIME))

    stage_before(E, STAGE_XS[0], "Schema Discovery",
                 "3–5 weeks", ["DBAs document tables,", "columns, relationships", "manually"])
    stage_before(E, STAGE_XS[1], "Data Documentation",
                 "2–4 weeks", ["Analysts write descriptions", "— often incomplete,", "always stale"])
    stage_before(E, STAGE_XS[2], "Column Mapping",
                 "2–3 weeks", ["Engineers map source", "to target in", "spreadsheets"])
    stage_before(E, STAGE_XS[3], "Quality Rules",
                 "1–3 weeks", ["Rules ad hoc per", "project — never reused", "or governed"])
    stage_before(E, STAGE_XS[4], "Deploy & Document",
                 "1–2 weeks", ["View built manually.", "No contract, no", "marketplace entry"])

    for i in range(4):
        mid_y = BEFORE_Y + BEFORE_H // 2
        E.extend(arrow(STAGE_XS[i] + STAGE_W, mid_y,
                       STAGE_XS[i + 1], mid_y, color=DB_TIME, stroke_width=1.5))

    resy = BEFORE_Y + 18
    E.extend(rect(RESULT_X, resy, RESULT_W, 100,
                  fill="#FEE2E2", stroke="#FCA5A5", stroke_width=1.5, rx=8))
    E.extend(text(RESULT_X + RESULT_W // 2, resy + 20, "Ungoverned",
                  font_size=9, font_weight="700", color=DB_TIME, text_anchor="middle"))
    E.extend(text(RESULT_X + RESULT_W // 2, resy + 36, "database table",
                  font_size=8, color=DB_BODY, text_anchor="middle"))
    E.extend(text(RESULT_X + RESULT_W // 2, resy + 64, "8–17",
                  font_size=20, font_weight="700", color=DB_TIME, text_anchor="middle"))
    E.extend(text(RESULT_X + RESULT_W // 2, resy + 84, "weeks",
                  font_size=9, color=DB_BODY, text_anchor="middle"))
    E.extend(arrow(STAGE_XS[4] + STAGE_W, BEFORE_Y + BEFORE_H // 2,
                   RESULT_X, BEFORE_Y + BEFORE_H // 2, color=DB_TIME, stroke_width=1.5))

    # ── Divider ──────────────────────────────────────────────────────────────
    E.extend(divider(40, DIV_Y, W - 40, DIV_Y, color="#CBD5E1", stroke_width=1.5))
    E.extend(rect(630, DIV_Y - 11, 180, 22, fill="#F8FAFC", stroke="#CBD5E1", rx=4))
    E.extend(text(720, DIV_Y, "AI agent skills →", font_size=10, font_weight="700",
                  color=P["skills_stroke"], text_anchor="middle"))

    # ── AFTER row ────────────────────────────────────────────────────────────
    E.extend(text(48, AFTER_Y - 12,
                  "WITH DATA WORKBENCH AGENT SKILLS  —  AI doing the data engineering work",
                  font_size=9, font_weight="700", color=DP_TIME))

    stage_after(E, STAGE_XS[0], "Auto-Discovery",
                "2–4 hrs", "data-discovery",
                ["Scans every table,", "column, FK — fully", "documented to graph"])
    stage_after(E, STAGE_XS[1], "Profile & Enrich",
                "1–3 hrs", "data-profiling + metadata-enrichment",
                ["AI writes descriptions", "from data patterns.", "Human approves."])
    stage_after(E, STAGE_XS[2], "Intelligent Mapping",
                "1–2 hrs", "data-mapping-neo4j",
                ["AI maps source→target", "with full lineage.", "Human reviews."])
    stage_after(E, STAGE_XS[3], "Quality & Rules",
                "1–2 hrs", "data-quality-rule-generation",
                ["Domain rules from", "catalog. Observation", "rules from profiling."])
    stage_after(E, STAGE_XS[4], "Serve & Publish",
                "< 1 hr", "data-serving-virtual-view",
                ["Virtual view + ODCS", "contract. Published", "to marketplace."])

    for i in range(4):
        mid_y = AFTER_Y + AFTER_H // 2
        E.extend(arrow(STAGE_XS[i] + STAGE_W, mid_y,
                       STAGE_XS[i + 1], mid_y, color=DP_TIME, stroke_width=2))

    resy2 = AFTER_Y + 8
    E.extend(rect(RESULT_X, resy2, RESULT_W, 152,
                  fill="#DCFCE7", stroke=DP_STROKE, stroke_width=2, rx=8))
    E.extend(text(RESULT_X + RESULT_W // 2, resy2 + 20, "Governed",
                  font_size=9, font_weight="700", color=DP_TIME, text_anchor="middle"))
    E.extend(text(RESULT_X + RESULT_W // 2, resy2 + 36, "Data Product",
                  font_size=9, color=DP_BODY, text_anchor="middle"))
    E.extend(text(RESULT_X + RESULT_W // 2, resy2 + 68, "Days",
                  font_size=20, font_weight="700", color=DP_TIME, text_anchor="middle"))
    checks = ["✓ Contract", "✓ Lineage", "✓ Quality", "✓ Marketplace", "✓ AI-Ready"]
    for i, ck in enumerate(checks):
        E.extend(text(RESULT_X + RESULT_W // 2, resy2 + 96 + i * 14, ck,
                      font_size=8, color=DP_BODY, text_anchor="middle"))
    E.extend(arrow(STAGE_XS[4] + STAGE_W, AFTER_Y + AFTER_H // 2,
                   RESULT_X, AFTER_Y + AFTER_H // 2, color=DP_TIME, stroke_width=2))

    # ── ENABLES ROW — What enterprise AI can do now ───────────────────────────
    E.extend(rect(40, ENABLES_Y, W - 80, ENABLES_H,
                  fill=EN_FILL, stroke=EN_STROKE, stroke_width=1.5, rx=8))
    E.extend(text(48, ENABLES_Y + 18,
                  "WHAT BECOMES POSSIBLE  —  enterprise AI with certified data products to reason over",
                  font_size=9, font_weight="700", color=EN_HEAD))

    enables = [
        ("Natural Language Queries",
         "Ask in business terms.\nGet governed answers."),
        ("Agentic Workflows",
         "Multi-step AI processes\ngrounded in certified data."),
        ("Consistent KPIs",
         "Semantic layer routes agents\nto same governed products."),
        ("AI-Powered Apps",
         "Applications built on\na trusted data foundation."),
    ]
    ew = (W - 80 - 16 - 3 * 12) // 4
    for i, (title, body) in enumerate(enables):
        ex = 48 + 16 + i * (ew + 12)
        E.extend(rect(ex, ENABLES_Y + 28, ew, ENABLES_H - 38,
                      fill="#DBEAFE", stroke="#93C5FD", rx=6))
        E.extend(text(ex + 10, ENABLES_Y + 42, title,
                      font_size=10, font_weight="700", color=EN_BODY))
        E.extend(text(ex + 10, ENABLES_Y + 56, body,
                      font_size=8, color="#2563EB"))

    # ── Stats row ────────────────────────────────────────────────────────────
    stat_items = [
        ("20+ Agent Skills",
         "Purpose-built for data engineering.\nEach with domain expertise."),
        ("Two Product Types",
         "Source-aligned: certify what you have.\nConsumer-aligned: build what you need."),
        ("Active Learning",
         "Every approval improves future runs.\nEvery rejection trains the system."),
        ("Days to AI-Ready",
         "From raw database to governed,\nmarketplace-ready data product."),
    ]
    sw = (W - 80 - 3 * 14) // 4
    for i, (title, body) in enumerate(stat_items):
        sx = 40 + i * (sw + 14)
        E.extend(rect(sx, STATS_Y, sw, STATS_H,
                      fill="#F1F5F9", stroke="#CBD5E1", stroke_width=1, rx=6))
        E.extend(text(sx + 14, STATS_Y + 22, title,
                      font_size=12, font_weight="700", color=P["title"]))
        E.extend(text(sx + 14, STATS_Y + 44, body,
                      font_size=10, color=P["body"]))

    # ── Footer ───────────────────────────────────────────────────────────────
    E.extend(rect(0, FOOTER_Y, W, H - FOOTER_Y, fill="#1E293B", stroke="none", rx=0))
    E.extend(text(W // 2, FOOTER_Y + 24,
                  "AI builds the foundation.  The foundation enables AI.",
                  font_size=14, font_weight="700", color="#F1F5F9",
                  text_anchor="middle"))
    E.extend(text(W // 2, FOOTER_Y + 48,
                  "The same agent technology that will run your business — "
                  "applied today to build the data it depends on.",
                  font_size=10, color="#64748B", text_anchor="middle"))

    save(E, "arc2_months_to_days_v2.svg", width=W, height=H, bg="#F8FAFC")
    print("✓  arc2_months_to_days_v2.svg")


build()
