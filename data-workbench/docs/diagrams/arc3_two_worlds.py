#!/usr/bin/env python3
"""
arc3_two_worlds.py — "The gap between what business wants and what data delivers is the most
expensive gap in any enterprise."
Executive brochure slide: dual-workbench model showing PO + Contract + Engineer collaboration.
Arc: Two Worlds, One Contract
"""
import sys, os
sys.path.insert(0, os.path.expanduser("~/.claude/skills/svg-architect/scripts"))
from svg_lib import *

W, H = 1440, 810

# Column layout
PO_X   = 40;  PO_W  = 430
CTR_X  = 502; CTR_W = 396
ENG_X  = 930; ENG_W = 470
COL_Y  = 95;  COL_H = 570

# Column fill + border
PO_FILL  = "#F5F3FF"; PO_BORDER  = "#7C3AED"; PO_HEADER  = "#7C3AED"
CTR_FILL = "#FFFBF0"; CTR_BORDER = "#D97706"; CTR_HEADER = "#D97706"
ENG_FILL = "#EFF6FF"; ENG_BORDER = "#2563EB"; ENG_HEADER = "#2563EB"

# Step cards inside columns
STEP_W = 390; STEP_H = 52

FOOTER_Y = 700


def workflow_step(E, xi, yi, label, detail="", fill="#FFFFFF", stroke="#CBD5E1",
                  lcolor=P["title"], dcolor=P["body"]):
    E.extend(rect(xi, yi, STEP_W, STEP_H, fill=fill, stroke=stroke,
                  stroke_width=1, rx=6))
    E.extend(text(xi + 14, yi + 18, label,
                  font_size=11, font_weight="700", color=lcolor))
    if detail:
        E.extend(text(xi + 14, yi + 36, detail, font_size=9, color=dcolor))


def contract_field(E, xi, yi, field, value, fill="#FFFDF5"):
    E.extend(rect(xi, yi, CTR_W - 32, 30, fill=fill, stroke="#FDE68A",
                  stroke_width=1, rx=4))
    E.extend(text(xi + 10, yi + 12, field,
                  font_size=8, font_weight="700", color="#92400E"))
    E.extend(text(xi + 10, yi + 26, value, font_size=8, color="#78350F"))


def build():
    E = []

    # ── Header ──────────────────────────────────────────────────────────────
    E.extend(rect(0, 0, W, 78, fill="#0F172A", stroke="none", rx=0))
    E.extend(text(48, 30, "The gap between what business wants and what data delivers is the most expensive gap in any enterprise.",
                  font_size=18, font_weight="700", color="#FFFFFF"))
    E.extend(text(48, 57, "Data Workbench gives each persona the right tool, connected by a contract both sides can trust.",
                  font_size=11, color="#94A3B8"))
    E.extend(rect(48, 75, 100, 3, fill=P["skills_stroke"], stroke="none", rx=1))

    # ════════════════════════════════════════════════════════════════════════
    # LEFT COLUMN — Product Owner
    # ════════════════════════════════════════════════════════════════════════
    E.extend(rect(PO_X, COL_Y, PO_W, COL_H, fill=PO_FILL, stroke=PO_BORDER,
                  stroke_width=2, rx=10))
    E.extend(header_band(PO_X, COL_Y, PO_W, 36, label="DATA PRODUCT OWNER",
                         fill=PO_HEADER, font_size=13, rx=10))
    E.extend(text(PO_X + 14, COL_Y + 52, "Contract-First Wizard",
                  font_size=10, color="#6D28D9"))

    PO_STEP_X = PO_X + 20
    steps_po = [
        ("1  Describe the idea",    "AI guide shapes rough notes into a product spec"),
        ("2  Author the schema",    "Select columns from domain catalogs + AI suggestions"),
        ("3  Set quality rules",    "Domain rules pre-loaded · add custom rules via chat"),
        ("4  Choose source inputs", "Select source-aligned products to consume"),
        ("5  Review & submit",      "AI readiness score · gap check · submit to engineer"),
    ]
    for i, (lbl, det) in enumerate(steps_po):
        workflow_step(E, PO_STEP_X, COL_Y + 72 + i * 62, lbl, det,
                      fill="#FFFFFF", stroke="#DDD6FE",
                      lcolor="#5B21B6", dcolor="#7C3AED")

    # AI guide callout
    E.extend(rect(PO_X + 14, COL_Y + COL_H - 68, PO_W - 28, 54,
                  fill="#EDE9FE", stroke="#C4B5FD", rx=6))
    E.extend(text(PO_X + 28, COL_Y + COL_H - 52, "AI Guide: product-authoring-assistant",
                  font_size=10, font_weight="700", color="#5B21B6"))
    E.extend(text(PO_X + 28, COL_Y + COL_H - 34, "Chat-driven schema authoring · Apply suggestions · Domain knowledge",
                  font_size=9, color="#7C3AED"))

    # ════════════════════════════════════════════════════════════════════════
    # CENTER COLUMN — The Contract
    # ════════════════════════════════════════════════════════════════════════
    E.extend(rect(CTR_X, COL_Y, CTR_W, COL_H, fill=CTR_FILL, stroke=CTR_BORDER,
                  stroke_width=2.5, rx=10))
    E.extend(header_band(CTR_X, COL_Y, CTR_W, 36, label="THE DATA CONTRACT",
                         fill=CTR_HEADER, font_size=13, rx=10))
    E.extend(text(CTR_X + 16, COL_Y + 52, "ODCS Standard · Versioned · Auditable",
                  font_size=9, font_weight="600", color="#D97706"))

    cf_x = CTR_X + 16
    contract_fields = [
        ("Schema",         "Column names · types · constraints · PK/FK"),
        ("Quality Rules",  "SHACL shapes · thresholds · valid values"),
        ("Lineage",        "Source products · mappings · transformations"),
        ("Governance",     "Owner · steward · lifecycle state"),
        ("Versioning",     "Draft → Approved → Published · PROV-O trail"),
        ("Discoverability","Marketplace listing · search · scoring"),
    ]
    for i, (field, value) in enumerate(contract_fields):
        contract_field(E, cf_x, COL_Y + 72 + i * 40, field, value)

    # "Handoff artifact" callout
    E.extend(rect(CTR_X + 16, COL_Y + COL_H - 80, CTR_W - 32, 66,
                  fill="#FEF3C7", stroke="#FCD34D", rx=6))
    E.extend(text(CTR_X + CTR_W // 2, COL_Y + COL_H - 60,
                  "The handoff artifact",
                  font_size=12, font_weight="700", color="#92400E",
                  text_anchor="middle"))
    E.extend(text(CTR_X + CTR_W // 2, COL_Y + COL_H - 40,
                  "both sides can trust",
                  font_size=12, font_weight="700", color="#92400E",
                  text_anchor="middle"))
    E.extend(text(CTR_X + CTR_W // 2, COL_Y + COL_H - 22,
                  "Neo4j Knowledge Graph · PROV-O Provenance",
                  font_size=8, color="#D97706", text_anchor="middle"))

    # ── Arrows between columns ───────────────────────────────────────────────
    arrow_y = COL_Y + COL_H // 2
    # PO → Contract
    E.extend(arrow(PO_X + PO_W, arrow_y, CTR_X, arrow_y,
                   color=PO_BORDER, stroke_width=2))
    E.extend(text((PO_X + PO_W + CTR_X) // 2, arrow_y - 10,
                  "defines", font_size=9, font_weight="700",
                  color=PO_BORDER, text_anchor="middle"))
    # Contract → Engineer
    E.extend(arrow(CTR_X + CTR_W, arrow_y, ENG_X, arrow_y,
                   color=ENG_BORDER, stroke_width=2))
    E.extend(text((CTR_X + CTR_W + ENG_X) // 2, arrow_y - 10,
                  "implements", font_size=9, font_weight="700",
                  color=ENG_BORDER, text_anchor="middle"))

    # ════════════════════════════════════════════════════════════════════════
    # RIGHT COLUMN — Data Engineer
    # ════════════════════════════════════════════════════════════════════════
    E.extend(rect(ENG_X, COL_Y, ENG_W, COL_H, fill=ENG_FILL, stroke=ENG_BORDER,
                  stroke_width=2, rx=10))
    E.extend(header_band(ENG_X, COL_Y, ENG_W, 36, label="DATA ENGINEER",
                         fill=ENG_HEADER, font_size=13, rx=10))
    E.extend(text(ENG_X + 14, COL_Y + 52, "Agent-Driven Engineering Workbench",
                  font_size=10, color="#1D4ED8"))

    ENG_STEP_X = ENG_X + 20
    STEP_W_ENG = ENG_W - 40
    steps_eng = [
        ("1  Discover & profile source data",   "Agents scan databases · profiling in hours"),
        ("2  Enrich descriptions",              "AI writes column descriptions · human approves"),
        ("3  Map source to target columns",     "AI mapping with full lineage · approve/replace"),
        ("4  Generate virtual serving views",   "SQL DDL auto-generated · FK-aware JOIN logic"),
        ("5  Deploy to data marketplace",       "Published · scored · discoverable by consumers"),
    ]
    for i, (lbl, det) in enumerate(steps_eng):
        E.extend(rect(ENG_STEP_X, COL_Y + 72 + i * 62, STEP_W_ENG, STEP_H,
                      fill="#FFFFFF", stroke="#BFDBFE", stroke_width=1, rx=6))
        E.extend(text(ENG_STEP_X + 14, COL_Y + 72 + i * 62 + 18, lbl,
                      font_size=11, font_weight="700", color="#1E3A8A"))
        E.extend(text(ENG_STEP_X + 14, COL_Y + 72 + i * 62 + 36, det,
                      font_size=9, color="#2563EB"))

    # Skills callout
    E.extend(rect(ENG_X + 14, COL_Y + COL_H - 68, ENG_W - 28, 54,
                  fill="#DBEAFE", stroke="#93C5FD", rx=6))
    E.extend(text(ENG_X + 28, COL_Y + COL_H - 52, "20+ Agent Skills",
                  font_size=10, font_weight="700", color="#1E3A8A"))
    E.extend(text(ENG_X + 28, COL_Y + COL_H - 34,
                  "discovery · profiling · enrichment · mapping · serving · quality · scoring",
                  font_size=9, color="#2563EB"))

    # ── Footer ───────────────────────────────────────────────────────────────
    E.extend(rect(0, FOOTER_Y, W, H - FOOTER_Y, fill="#1E293B", stroke="none", rx=0))

    # Three key messages
    msgs = [
        ("Product Owner defines.",  "Business intent captured in\na structured data contract."),
        ("Engineer implements.",    "Agent skills do the heavy\nlifting — hours, not weeks."),
        ("Both sides stay aligned.", "Contract is the shared artifact.\nAI is the accelerator."),
    ]
    msg_w = (W - 80 - 2 * 20) // 3
    for i, (bold, body) in enumerate(msgs):
        mx = 40 + i * (msg_w + 20)
        E.extend(text(mx + 14, FOOTER_Y + 30, bold,
                      font_size=13, font_weight="700", color="#F1F5F9"))
        E.extend(text(mx + 14, FOOTER_Y + 54, body,
                      font_size=10, color="#94A3B8"))
        if i < 2:
            E.extend(divider(mx + msg_w + 10, FOOTER_Y + 20, mx + msg_w + 10, H - 20,
                             color="#334155"))

    save(E, "arc3_two_worlds.svg", width=W, height=H, bg="#F8FAFC")
    print("✓  arc3_two_worlds.svg")


build()
