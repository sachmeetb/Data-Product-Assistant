#!/usr/bin/env python3
"""
arc3_data_product_stack_v2.py
"AI builds the foundation. The foundation powers AI."
Symmetric three-column: AI as Builder (left) | Data Products (center) | AI as Consumer (right)
The same agent technology that will run your business is being used to build the data it depends on.
"""
import sys, os
sys.path.insert(0, os.path.expanduser("~/.claude/skills/svg-architect/scripts"))
from svg_lib import *

W, H = 1440, 810

# Three-column layout
LEFT_X  = 40;   LEFT_W  = 420
CTR_X   = 480;  CTR_W   = 480
RIGHT_X = 980;  RIGHT_W = 420
COL_Y   = 92;   COL_H   = 606
FOOTER_Y = 714

# Accent colors per column
LEFT_FILL   = "#F0FFF4"; LEFT_STROKE = "#86EFAC"; LEFT_HEAD = "#16A34A"; LEFT_DARK = "#14532D"
CTR_FILL    = "#FFFBF0"; CTR_STROKE  = "#D97706"; CTR_HEAD  = "#D97706"; CTR_DARK  = "#78350F"
RIGHT_FILL  = "#EFF6FF"; RIGHT_STROKE= "#93C5FD"; RIGHT_HEAD= "#2563EB"; RIGHT_DARK= "#1E3A8A"


def left_item(E, xi, yi, w, num, title, desc):
    """Numbered skill item in the left builder column."""
    E.extend(rect(xi, yi, 26, 26, fill=LEFT_HEAD, stroke="none", rx=13))
    E.extend(text(xi + 13, yi + 13, str(num),
                  font_size=9, font_weight="700", color="#FFFFFF", text_anchor="middle"))
    E.extend(text(xi + 34, yi + 10, title,
                  font_size=11, font_weight="700", color=LEFT_DARK))
    E.extend(text(xi + 34, yi + 26, desc,
                  font_size=9, color=P["body"]))


def right_item(E, xi, yi, w, title, body_lines):
    """Use-case item in the right consumer column."""
    E.extend(rect(xi, yi, w, 76, fill=RIGHT_FILL, stroke=RIGHT_STROKE, rx=6))
    E.extend(text(xi + 14, yi + 18, title,
                  font_size=11, font_weight="700", color=RIGHT_DARK))
    for i, ln in enumerate(body_lines):
        E.extend(text(xi + 14, yi + 35 + i * 15, ln,
                      font_size=9, color="#2563EB"))


def build():
    E = []

    # ── Header ──────────────────────────────────────────────────────────────
    E.extend(rect(0, 0, W, 82, fill="#0F172A", stroke="none", rx=0))
    E.extend(text(W // 2, 26,
                  "AI builds the foundation.  The foundation powers AI.",
                  font_size=22, font_weight="700", color="#FFFFFF",
                  text_anchor="middle"))
    E.extend(text(W // 2, 56,
                  "The same AI agent technology applied to the domain of data engineering — "
                  "building what enterprise AI will run on.",
                  font_size=10, color="#94A3B8", text_anchor="middle"))
    E.extend(rect(W // 2 - 80, 78, 160, 3, fill=P["skills_stroke"], stroke="none", rx=1))

    # ════════════════════════════════════════════════════════════════════════
    # LEFT COLUMN — AI as the BUILDER
    # ════════════════════════════════════════════════════════════════════════
    E.extend(rect(LEFT_X, COL_Y, LEFT_W, COL_H, fill=LEFT_FILL,
                  stroke=LEFT_STROKE, stroke_width=2, rx=10))
    E.extend(header_band(LEFT_X, COL_Y, LEFT_W, 36,
                         label="AI AS THE BUILDER",
                         fill=LEFT_HEAD, font_size=13, rx=10,
                         sub_label="Data Workbench",
                         sub_font_size=9, sub_color="rgba(255,255,255,0.85)"))
    E.extend(text(LEFT_X + 16, COL_Y + 52,
                  "Data Workbench applies AI agent",
                  font_size=10, font_weight="600", color=LEFT_DARK))
    E.extend(text(LEFT_X + 16, COL_Y + 66,
                  "technology to data engineering:",
                  font_size=10, font_weight="600", color=LEFT_DARK))

    skills = [
        ("Schema Discovery",
         "Agents scan databases, discover every table,\ncolumn, FK, and index — hours, not weeks"),
        ("Data Profiling",
         "Statistical patterns, nulls, cardinality,\nanomalies — data quality baseline built fast"),
        ("Metadata Enrichment",
         "AI writes business descriptions from data\npatterns — human approves or corrects each"),
        ("Intelligent Mapping",
         "Source-to-target column mapping with full\nlineage — AI suggests, human approves"),
        ("Quality Assurance",
         "Domain rules from knowledge catalogs +\nobservation rules derived from profiling"),
        ("Serve & Publish",
         "Virtual views + ODCS contract generated +\nmarketplace listing in under a day"),
    ]
    ly = COL_Y + 84
    for idx, (title, desc) in enumerate(skills):
        left_item(E, LEFT_X + 16, ly, LEFT_W - 32, idx + 1, title, desc)
        if idx < len(skills) - 1:
            E.extend(divider(LEFT_X + 18, ly + 48, LEFT_X + LEFT_W - 18, ly + 48,
                             color="#DCFCE7"))
        ly += 72

    # Callout at bottom of left panel
    cally = COL_Y + COL_H - 60
    E.extend(rect(LEFT_X + 14, cally, LEFT_W - 28, 48,
                  fill="#DCFCE7", stroke=LEFT_STROKE, rx=6))
    E.extend(text(LEFT_X + 24, cally + 16,
                  "Human-in-the-loop at every decision",
                  font_size=9, font_weight="700", color=LEFT_DARK))
    E.extend(text(LEFT_X + 24, cally + 30,
                  "Active Learning  ·  PROV-O Provenance  ·  Neo4j Graph",
                  font_size=8, color=LEFT_HEAD))

    # ════════════════════════════════════════════════════════════════════════
    # CENTER COLUMN — Data Products (the bridge)
    # ════════════════════════════════════════════════════════════════════════
    E.extend(rect(CTR_X, COL_Y, CTR_W, COL_H, fill=CTR_FILL,
                  stroke=CTR_STROKE, stroke_width=2.5, rx=10))
    E.extend(header_band(CTR_X, COL_Y, CTR_W, 36,
                         label="DATA PRODUCTS  —  THE BRIDGE",
                         fill=CTR_HEAD, font_size=13, rx=10))

    # "Built by" / "Consumed by" indicators
    E.extend(text(CTR_X + CTR_W // 4, COL_Y + 54,
                  "← built by AI agents",
                  font_size=9, font_weight="600", color=LEFT_HEAD,
                  text_anchor="middle"))
    E.extend(text(CTR_X + 3 * CTR_W // 4, COL_Y + 54,
                  "consumed by AI agents →",
                  font_size=9, font_weight="600", color=RIGHT_HEAD,
                  text_anchor="middle"))

    # Divider under the indicators
    E.extend(divider(CTR_X + 16, COL_Y + 64, CTR_X + CTR_W - 16, COL_Y + 64,
                     color="#FDE68A"))

    # Two product types side by side
    hw = (CTR_W - 32 - 8) // 2
    sa_x = CTR_X + 16
    cf_x = CTR_X + 16 + hw + 8

    for (bx, btype, sub, bullets) in [
        (sa_x, "SOURCE-ALIGNED", "Certify what you have",
         ["Existing databases & warehouses",
          "Auto-discovered, profiled, enriched",
          "ODCS contract synthesized from graph",
          "Published with quality rules + lineage"]),
        (cf_x, "CONSUMER-ALIGNED", "Build what you need",
         ["Contract-first schema authoring",
          "PO defines before engineer builds",
          "Mapped to source products via graph",
          "AI-ready: governed, semantically grounded"]),
    ]:
        E.extend(rect(bx, COL_Y + 72, hw, 148, fill="#DCFCE7", stroke=LEFT_STROKE, rx=6))
        E.extend(text(bx + hw // 2, COL_Y + 92, btype,
                      font_size=10, font_weight="700", color=LEFT_DARK,
                      text_anchor="middle"))
        E.extend(text(bx + hw // 2, COL_Y + 108, sub,
                      font_size=8, color=LEFT_HEAD, text_anchor="middle"))
        for i, b in enumerate(bullets):
            E.extend(text(bx + 10, COL_Y + 124 + i * 18, f"→  {b}",
                          font_size=8, color=LEFT_DARK))

    # Properties
    props_y = COL_Y + 232
    props = ["Contracted (ODCS)", "Governed", "Lineage-traced",
             "Quality-assured", "Marketplace-discoverable"]
    pw = (CTR_W - 32 - 4 * 6) // 5
    for i, prop in enumerate(props):
        px = CTR_X + 16 + i * (pw + 6)
        E.extend(rect(px, props_y, pw, 22, fill="#FEF3C7", stroke="#FCD34D", rx=11))
        E.extend(text(px + pw // 2, props_y + 11, prop,
                      font_size=7, font_weight="700", color="#92400E",
                      text_anchor="middle"))

    # The domain-of-data framing
    E.extend(rect(CTR_X + 16, COL_Y + 266, CTR_W - 32, 72,
                  fill="#1E293B", stroke="none", rx=8))
    E.extend(text(CTR_X + CTR_W // 2, COL_Y + 286,
                  "AI applied to every domain.",
                  font_size=13, font_weight="700", color="#F1F5F9",
                  text_anchor="middle"))
    E.extend(text(CTR_X + CTR_W // 2, COL_Y + 308,
                  "Data Workbench applies it to the domain of DATA.",
                  font_size=10, color="#94A3B8", text_anchor="middle"))
    E.extend(text(CTR_X + CTR_W // 2, COL_Y + 326,
                  "Purpose-built agent skills for data engineering.",
                  font_size=9, color="#64748B", text_anchor="middle"))

    # Semantic layer bridge note
    E.extend(rect(CTR_X + 16, COL_Y + 350, CTR_W - 32, 48,
                  fill="#FFFDE7", stroke="#FCD34D", rx=6))
    E.extend(text(CTR_X + 24, COL_Y + 368,
                  "Semantic layer  (between data products and AI consumers):",
                  font_size=9, font_weight="700", color="#92400E"))
    E.extend(text(CTR_X + 24, COL_Y + 384,
                  "Routes AI queries to the right certified data product.  "
                  "Proof-of-concept included in Data Workbench.",
                  font_size=8, color="#D97706"))

    # Virtuous cycle
    E.extend(rect(CTR_X + 16, COL_Y + 406, CTR_W - 32, 72,
                  fill="#F0FFF4", stroke="#86EFAC", rx=6))
    E.extend(text(CTR_X + CTR_W // 2, COL_Y + 422,
                  "The Virtuous Cycle",
                  font_size=11, font_weight="700", color=LEFT_DARK,
                  text_anchor="middle"))
    cycle_lines = [
        "Better data products → better AI outcomes",
        "Better AI outcomes → more enterprise confidence",
        "More confidence → more data products → stronger foundation",
    ]
    for i, ln in enumerate(cycle_lines):
        E.extend(text(CTR_X + 24, COL_Y + 438 + i * 16, f"↺  {ln}",
                      font_size=8, color="#16A34A"))

    # Stats  (starts 16px after virtuous cycle bottom at COL_Y+478)
    E.extend(rect(CTR_X + 16, COL_Y + COL_H - 100, CTR_W - 32, 46,
                  fill="#FEF9F0", stroke="#FED7AA", rx=6))
    E.extend(text(CTR_X + CTR_W // 2, COL_Y + COL_H - 84,
                  "83% of orgs don't build business-aligned data products",
                  font_size=9, font_weight="600", color="#92400E",
                  text_anchor="middle"))
    E.extend(text(CTR_X + CTR_W // 2, COL_Y + COL_H - 68,
                  "Only 9% have a functioning AI data readiness assessment",
                  font_size=9, color="#78350F", text_anchor="middle"))
    E.extend(text(CTR_X + CTR_W // 2, COL_Y + COL_H - 54,
                  "Gartner 2024 Survey — D&A Leaders, Ready Your Data for AI",
                  font_size=8, color="#D97706", text_anchor="middle"))

    # Mini flow at very bottom of center
    flow_y = COL_Y + COL_H - 48
    flow_items  = ["Source DB", "Agent Skills", "Data Product", "Semantic", "AI Agents"]
    flow_fills  = ["#F1F5F9", "#DCFCE7", "#FEF3C7", "#FFFDE7", "#DBEAFE"]
    flow_colors = ["#475569", "#14532D", "#92400E", "#D97706", "#1E3A8A"]
    fw = CTR_W - 32
    fi_w = (fw - 4 * 4) // 5
    for i, (item, fill, color) in enumerate(zip(flow_items, flow_fills, flow_colors)):
        fix = CTR_X + 16 + i * (fi_w + 4)
        E.extend(rect(fix, flow_y, fi_w, 26, fill=fill, stroke="#E2E8F0", rx=3))
        E.extend(text(fix + fi_w // 2, flow_y + 13, item,
                      font_size=6, color=color, text_anchor="middle"))
        if i < 4:
            E.extend(text(fix + fi_w + 2, flow_y + 13, "→",
                          font_size=7, color=P["muted"], text_anchor="middle"))

    # ── Connecting arrows in gaps ─────────────────────────────────────────────
    arr_y = COL_Y + COL_H // 2
    E.extend(arrow(LEFT_X + LEFT_W + 4, arr_y, CTR_X - 4, arr_y,
                   color=LEFT_HEAD, stroke_width=2.5))
    E.extend(text((LEFT_X + LEFT_W + CTR_X) // 2, arr_y - 12,
                  "builds →", font_size=9, font_weight="700",
                  color=LEFT_HEAD, text_anchor="middle"))

    E.extend(arrow(CTR_X + CTR_W + 4, arr_y, RIGHT_X - 4, arr_y,
                   color=RIGHT_HEAD, stroke_width=2.5))
    E.extend(text((CTR_X + CTR_W + RIGHT_X) // 2, arr_y - 12,
                  "→ enables", font_size=9, font_weight="700",
                  color=RIGHT_HEAD, text_anchor="middle"))

    # ════════════════════════════════════════════════════════════════════════
    # RIGHT COLUMN — AI as the CONSUMER
    # ════════════════════════════════════════════════════════════════════════
    E.extend(rect(RIGHT_X, COL_Y, RIGHT_W, COL_H, fill=RIGHT_FILL,
                  stroke=RIGHT_STROKE, stroke_width=2, rx=10))
    E.extend(header_band(RIGHT_X, COL_Y, RIGHT_W, 36,
                         label="AI AS THE CONSUMER",
                         fill=RIGHT_HEAD, font_size=13, rx=10,
                         sub_label="Enterprise AI",
                         sub_font_size=9, sub_color="rgba(255,255,255,0.85)"))
    E.extend(text(RIGHT_X + 16, COL_Y + 52,
                  "Enterprise AI that needs certified,",
                  font_size=10, font_weight="600", color=RIGHT_DARK))
    E.extend(text(RIGHT_X + 16, COL_Y + 66,
                  "governed data to reason correctly:",
                  font_size=10, font_weight="600", color=RIGHT_DARK))

    use_cases = [
        ("Natural Language Queries",
         ["Ask in business terms.",
          "Agent routes to the right",
          "certified data product."]),
        ("Agentic Workflows",
         ["Multi-step AI processes",
          "grounded in governed data.",
          "No hallucination at scale."]),
        ("Automated Analytics",
         ["Agents reason directly over",
          "data products. Consistent",
          "KPIs, no conflicting defs."]),
        ("AI-Powered Applications",
         ["Apps with semantic context",
          "baked in — built on a",
          "certified data foundation."]),
    ]
    ry = COL_Y + 84
    for title, body_lines in use_cases:
        right_item(E, RIGHT_X + 16, ry, RIGHT_W - 32, title, body_lines)
        ry += 86

    # Gartner stat
    gy = COL_Y + COL_H - 130
    E.extend(rect(RIGHT_X + 14, gy, RIGHT_W - 28, 118,
                  fill="#DBEAFE", stroke="#93C5FD", rx=8))
    E.extend(text(RIGHT_X + RIGHT_W // 2, gy + 28,
                  "80%", font_size=30, font_weight="700",
                  color=RIGHT_HEAD, text_anchor="middle"))
    E.extend(text(RIGHT_X + 28, gy + 56,
                  "higher agentic AI accuracy",
                  font_size=9, font_weight="700", color=RIGHT_DARK))
    E.extend(text(RIGHT_X + 28, gy + 70,
                  "60% lower cost",
                  font_size=9, font_weight="700", color=RIGHT_DARK))
    E.extend(text(RIGHT_X + 28, gy + 86,
                  "when organizations prioritize",
                  font_size=8, color=RIGHT_DARK))
    E.extend(text(RIGHT_X + 28, gy + 100,
                  "semantics in AI-ready data.",
                  font_size=8, color=RIGHT_DARK))
    E.extend(text(RIGHT_X + 28, gy + 114,
                  "Gartner  ·  May 2026",
                  font_size=7, color="#2563EB"))

    # ── Footer ───────────────────────────────────────────────────────────────
    E.extend(rect(0, FOOTER_Y, W, H - FOOTER_Y, fill="#0F172A", stroke="none", rx=0))
    E.extend(text(W // 2, FOOTER_Y + 30,
                  "The agents building the foundation.  "
                  "The foundation enabling the agents.",
                  font_size=17, font_weight="700", color="#F1F5F9",
                  text_anchor="middle"))
    E.extend(text(W // 2, FOOTER_Y + 58,
                  "AI innovation applied to the domain of data — "
                  "so data can power everything else.",
                  font_size=11, color="#94A3B8", text_anchor="middle"))
    E.extend(divider(W // 2 - 300, FOOTER_Y + 74,
                     W // 2 + 300, FOOTER_Y + 74, color="#1E293B"))
    E.extend(text(W // 2, FOOTER_Y + 94,
                  "What would your enterprise AI look like with a certified, "
                  "governed data foundation ready on day one?",
                  font_size=11, font_weight="600", color="#64748B",
                  text_anchor="middle"))

    save(E, "arc3_data_product_stack_v2.svg", width=W, height=H, bg="#F8FAFC")
    print("✓  arc3_data_product_stack_v2.svg")


build()
