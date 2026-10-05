#!/usr/bin/env python3
"""
arc1_ai_readiness_gap.py — v2
"Algorithms are off the shelf. Differentiation is in the data."
Two-column: AI readiness gap (left) + Data Workbench closes it (right).
"""
import sys, os
sys.path.insert(0, os.path.expanduser("~/.claude/skills/svg-architect/scripts"))
from svg_lib import *

W, H = 1440, 810

LEFT_X  = 40;   LEFT_W  = 672
RIGHT_X = 740;  RIGHT_W = 660
COL_Y   = 92
FOOTER_Y = 692

PROB_H = 110; PROB_GAP = 14
CAP_H  = 158; CAP_GAP  = 18


def prob_card(E, xi, yi, stat, title, body):
    """Orange-accented problem card with optional stat headline."""
    E.extend(rect(xi, yi, LEFT_W, PROB_H, fill="#FFF8F2", stroke="#FED7AA",
                  stroke_width=1.5, rx=8))
    E.extend(rect(xi, yi, 4, PROB_H, fill="#F97316", stroke="none", rx=2))
    if stat:
        E.extend(text(xi + 20, yi + 28, stat,
                      font_size=26, font_weight="700", color="#F97316"))
        E.extend(text(xi + 20, yi + 54, title,
                      font_size=11, font_weight="700", color="#92400E"))
        E.extend(text(xi + 20, yi + 72, body,
                      font_size=10, color="#78350F"))
    else:
        E.extend(text(xi + 20, yi + 28, title,
                      font_size=13, font_weight="700", color="#92400E"))
        E.extend(text(xi + 20, yi + 54, body,
                      font_size=10, color="#78350F"))


def cap_card(E, xi, yi, header, body_lines, skills=""):
    """Green capability card with optional skill footer."""
    E.extend(rect(xi, yi, RIGHT_W, CAP_H, fill=P["skills_fill"], stroke="#86EFAC",
                  stroke_width=2, rx=8))
    E.extend(header_band(xi, yi, RIGHT_W, 30, label=header, fill=P["skills_stroke"],
                         font_size=12, rx=8))
    E.extend(text(xi + 18, yi + 46, "\n".join(body_lines), font_size=11, color=P["body"]))
    if skills:
        E.extend(divider(xi + 14, yi + CAP_H - 30, xi + RIGHT_W - 14, yi + CAP_H - 30))
        E.extend(text(xi + 18, yi + CAP_H - 18, f"Skills: {skills}",
                      font_size=8, color=P["muted"]))


def build():
    E = []

    # ── Header ──────────────────────────────────────────────────────────────
    E.extend(rect(0, 0, W, 82, fill="#0F172A", stroke="none", rx=0))
    E.extend(text(48, 28, "Algorithms are off the shelf. Differentiation is in the data.",
                  font_size=22, font_weight="700", color="#FFFFFF"))
    E.extend(text(48, 57,
                  "77% of D&A leaders cite AI-ready data as a top-5 investment priority.  "
                  "Only 9% have a functioning data readiness assessment.",
                  font_size=10, color="#94A3B8"))
    E.extend(rect(48, 78, 120, 3, fill=P["skills_stroke"], stroke="none", rx=1))

    # ── Section labels ───────────────────────────────────────────────────────
    E.extend(text(LEFT_X, COL_Y - 10, "THE AI READINESS GAP",
                  font_size=9, font_weight="700", color="#F97316"))
    E.extend(text(RIGHT_X, COL_Y - 10, "HOW DATA WORKBENCH CLOSES IT",
                  font_size=9, font_weight="700", color=P["skills_stroke"]))

    # ── Vertical divider ─────────────────────────────────────────────────────
    E.extend(divider(RIGHT_X - 18, 86, RIGHT_X - 18, FOOTER_Y - 10, color="#E2E8F0"))

    # ── Quote callout (top of left column) ──────────────────────────────────
    E.extend(rect(LEFT_X, COL_Y, LEFT_W, 52, fill="#1E293B", stroke="none", rx=6))
    E.extend(text(LEFT_X + 18, COL_Y + 18,
                  "\"You have a database, not a data product.\"",
                  font_size=14, font_weight="700", color="#F1F5F9"))
    E.extend(text(LEFT_X + 18, COL_Y + 40,
                  "— Tony Giordano, Data Workbench Practice Lead",
                  font_size=9, color="#64748B"))

    # ── Problem cards ────────────────────────────────────────────────────────
    problems = [
        ("83%", "of organizations don't develop business-aligned data products",
         "Most enterprise data exists as raw, undocumented tables — invisible to AI,\n"
         "lacking business context, and unfit for governed consumption."),
        ("9%", "have a functioning AI data readiness assessment",
         "Organizations are planning AI initiatives without knowing which data assets\n"
         "are ready to support them. The gap between ambition and readiness stalls AI."),
        (None, "Semantic fragmentation causes AI to reason incorrectly",
         "\"Customer\", \"Client\", \"Account\" — conflicting definitions across\n"
         "teams cause AI to hallucinate business answers in production."),
    ]
    py = COL_Y + 62
    for stat, title, body in problems:
        prob_card(E, LEFT_X, py, stat, title, body)
        py += PROB_H + PROB_GAP

    # ── Capability cards (right column) ─────────────────────────────────────
    caps = [
        ("DISCOVER & UNDERSTAND AUTOMATICALLY",
         ["Agent skills scan every schema, profile every column,",
          "and write business descriptions from data patterns.",
          "Hours to a complete, governed semantic knowledge graph."],
         "data-discovery  ·  data-profiling  ·  metadata-enrichment"),
        ("CONTRACT, GOVERN & QUALIFY",
         ["ODCS data contracts encode business rules, quality thresholds,",
          "and lineage for every data asset.",
          "Domain knowledge catalogs pre-load governance rules per domain."],
         "domain-rule-enhancement  ·  data-quality-rule-generation"),
        ("MAP, SERVE & PUBLISH",
         ["AI maps source columns to target schemas with full lineage.",
          "Virtual serving views generated, tested, and deployed.",
          "Published to a searchable data marketplace — days, not months."],
         "data-mapping-neo4j  ·  data-serving-virtual-view  ·  data-scoring"),
    ]
    cy = COL_Y
    for header, body_lines, skills in caps:
        cap_card(E, RIGHT_X, cy, header, body_lines, skills)
        cy += CAP_H + CAP_GAP

    # ── Footer ───────────────────────────────────────────────────────────────
    E.extend(rect(0, FOOTER_Y, W, H - FOOTER_Y, fill="#1E293B", stroke="none", rx=0))
    E.extend(text(
        W // 2, FOOTER_Y + 26,
        "\"By 2027, organizations prioritizing semantics in AI-ready data will achieve "
        "80% higher agentic AI accuracy and 60% lower cost.\"",
        font_size=12, font_weight="600", color="#F1F5F9", text_anchor="middle"))
    E.extend(text(
        W // 2, FOOTER_Y + 52,
        "Gartner  ·  Prioritize Semantics to Overcome the Agentic Contextualization Gap"
        "  ·  May 2026  ·  ID G00852605",
        font_size=9, color="#64748B", text_anchor="middle"))
    E.extend(divider(W // 2 - 320, FOOTER_Y + 66,
                     W // 2 + 320, FOOTER_Y + 66, color="#334155"))
    E.extend(text(
        W // 2, FOOTER_Y + 88,
        "The question isn't whether to build data products. "
        "It's whether you can do it fast enough.",
        font_size=11, font_weight="600", color="#94A3B8", text_anchor="middle"))

    save(E, "arc1_ai_readiness_gap.svg", width=W, height=H, bg="#F8FAFC")
    print("✓  arc1_ai_readiness_gap.svg")


build()
