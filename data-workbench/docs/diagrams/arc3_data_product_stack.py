#!/usr/bin/env python3
"""
arc3_data_product_stack.py
"The three layers every AI initiative needs. Start with the foundation."
Architectural stack: Data Products → Semantic Layer → AI Agents.
Data Workbench builds Layer 1 — completely.
"""
import sys, os
sys.path.insert(0, os.path.expanduser("~/.claude/skills/svg-architect/scripts"))
from svg_lib import *

W, H = 1440, 810

# Label column (left side, shared across all layers)
LABEL_X  = 40
LABEL_W  = 230
DIVIDER_X = LABEL_X + LABEL_W + 12   # vertical divider between label + content
CONTENT_X = DIVIDER_X + 12
CONTENT_W = W - CONTENT_X - 40

# Layer geometry (top = AI Agents, bottom = Data Products, visual stack)
L3_Y, L3_H   = 82,  124     # AI Agents & Applications
L2_Y, L2_H   = 226, 154     # Semantic Layer
L1_Y, L1_H   = 400, 252     # Data Products (featured, largest)
SRC_Y, SRC_H = 672, 46      # Source Systems (dark band)
FOOTER_Y      = 726

# Layer color themes
L3_FILL  = "#EFF6FF"; L3_STROKE = "#2563EB"; L3_ACCENT = "#2563EB"; L3_DARK = "#1E3A8A"
L2_FILL  = "#FFFBF0"; L2_STROKE = "#D97706"; L2_ACCENT = "#D97706"; L2_DARK = "#92400E"
L1_FILL  = "#F0FFF4"; L1_STROKE = "#16A34A"; L1_ACCENT = "#16A34A"; L1_DARK = "#14532D"


def layer_label(E, ly, lh, num, name_lines, role, accent, dark):
    """Left-column label for a layer."""
    cy = ly + lh // 2
    # Layer number badge
    E.extend(rect(LABEL_X, cy - 26, 46, 18, fill=accent, stroke="none", rx=9))
    E.extend(text(LABEL_X + 23, cy - 17, f"Layer {num}",
                  font_size=7, font_weight="700", color="#FFFFFF", text_anchor="middle"))
    # Layer name
    for i, ln in enumerate(name_lines):
        E.extend(text(LABEL_X, cy + 2 + i * 16, ln,
                      font_size=13, font_weight="700", color=dark))
    # Role tag
    E.extend(text(LABEL_X, cy + 2 + len(name_lines) * 16 + 6, role,
                  font_size=9, color=P["body"]))


def content_box(E, bx, by, bw, bh, title, body_lines, fill, stroke, tcolor, bcolor):
    """Small info box inside a layer's content zone."""
    E.extend(rect(bx, by, bw, bh, fill=fill, stroke=stroke, stroke_width=1.5, rx=6))
    E.extend(text(bx + 12, by + 18, title,
                  font_size=10, font_weight="700", color=tcolor))
    for i, ln in enumerate(body_lines):
        E.extend(text(bx + 12, by + 36 + i * 14, ln,
                      font_size=9, color=bcolor))


def build():
    E = []

    # ── Header ──────────────────────────────────────────────────────────────
    E.extend(rect(0, 0, W, 74, fill="#0F172A", stroke="none", rx=0))
    E.extend(text(48, 26,
                  "The three layers every AI initiative needs. Start with the foundation.",
                  font_size=20, font_weight="700", color="#FFFFFF"))
    E.extend(text(48, 54,
                  "AI agents are only as good as the data they reason over.  "
                  "Reliable AI begins with reliable, governed data products.",
                  font_size=10, color="#94A3B8"))
    E.extend(rect(48, 70, 120, 3, fill=L1_ACCENT, stroke="none", rx=1))

    # ════════════════════════════════════════════════════════════════════════
    # LAYER 3 — AI Agents & Applications
    # ════════════════════════════════════════════════════════════════════════
    E.extend(rect(0, L3_Y, W, L3_H, fill=L3_FILL, stroke="none", rx=0))
    E.extend(divider(0, L3_Y, W, L3_Y, color=L3_STROKE, stroke_width=1))
    layer_label(E, L3_Y, L3_H, 3, ["AI AGENTS &", "APPLICATIONS"], "The Consumer",
                L3_ACCENT, L3_DARK)
    E.extend(divider(DIVIDER_X, L3_Y + 10, DIVIDER_X, L3_Y + L3_H - 10, color="#CBD5E1"))

    # L3 content: 4 boxes
    bw3 = (CONTENT_W - 3 * 14) // 4
    l3_boxes = [
        ("Natural Language Queries",
         ["Ask in business terms.", "Get trusted, governed answers."]),
        ("Agentic Workflows",
         ["Multi-step AI processes grounded", "in certified data products."]),
        ("Automated Analytics",
         ["Agents reason over data directly.", "No manual query engineering."]),
        ("AI-Powered Applications",
         ["Apps with semantic context.", "Consistent KPIs across tools."]),
    ]
    for i, (title, body) in enumerate(l3_boxes):
        bx = CONTENT_X + i * (bw3 + 14)
        content_box(E, bx, L3_Y + 12, bw3, L3_H - 24,
                    title, body, "#DBEAFE", "#93C5FD", L3_DARK, L3_ACCENT)

    # ── Gap indicator L2→L3 (confined to 20px gap y=206-226) ────────────────
    gap_cx = LABEL_X + LABEL_W // 2
    E.extend(arrow(gap_cx, 222, gap_cx, 210, color=L2_ACCENT, stroke_width=2))
    E.extend(text(gap_cx + 8, 218, "enables", font_size=7, color=P["muted"]))

    # ════════════════════════════════════════════════════════════════════════
    # LAYER 2 — Semantic Layer
    # ════════════════════════════════════════════════════════════════════════
    E.extend(rect(0, L2_Y, W, L2_H, fill=L2_FILL, stroke="none", rx=0))
    E.extend(divider(0, L2_Y, W, L2_Y, color=L2_STROKE, stroke_width=1))
    layer_label(E, L2_Y, L2_H, 2, ["SEMANTIC LAYER"], "The Router",
                L2_ACCENT, L2_DARK)
    E.extend(divider(DIVIDER_X, L2_Y + 10, DIVIDER_X, L2_Y + L2_H - 10, color="#CBD5E1"))

    # L2 content: 3 wider boxes + POC note
    bw2 = (CONTENT_W - 2 * 14) // 3
    l2_boxes = [
        ("Business Ontology  (Top Layer)",
         ["Maps business concepts and terms.",
          "Ensures consistent KPIs,",
          "metrics, and definitions."]),
        ("Data Product Metadata  (Bottom Layer)",
         ["Maps ontology concepts",
          "to certified data products.",
          "Bridges business to technical."]),
        ("Semantic Routing + Context",
         ["Routes AI queries to the right",
          "data product automatically.",
          "Injects semantic context into prompts."]),
    ]
    for i, (title, body) in enumerate(l2_boxes):
        bx = CONTENT_X + i * (bw2 + 14)
        content_box(E, bx, L2_Y + 10, bw2, L2_H - 44,
                    title, body, "#FEF3C7", "#FCD34D", L2_DARK, L2_ACCENT)

    # POC banner in L2
    E.extend(rect(CONTENT_X, L2_Y + L2_H - 30, CONTENT_W, 24,
                  fill="#FEFCE8", stroke="#FDE68A", rx=4))
    E.extend(text(CONTENT_X + 16, L2_Y + L2_H - 18,
                  "Proof-of-concept dual-ontology semantic layer included in Data Workbench"
                  " — demonstrates how governed data products directly power AI queries",
                  font_size=9, color="#92400E"))

    # ── Gap indicator L1→L2 (confined to 20px gap y=380-400) ────────────────
    E.extend(arrow(gap_cx, 396, gap_cx, 384, color=L1_ACCENT, stroke_width=2))
    E.extend(text(gap_cx + 8, 392, "powers", font_size=7, color=P["muted"]))

    # ════════════════════════════════════════════════════════════════════════
    # LAYER 1 — Data Products (featured)
    # ════════════════════════════════════════════════════════════════════════
    E.extend(rect(0, L1_Y, W, L1_H, fill=L1_FILL, stroke="none", rx=0))
    E.extend(divider(0, L1_Y, W, L1_Y, color=L1_STROKE, stroke_width=2.5))
    layer_label(E, L1_Y, L1_H, 1, ["DATA PRODUCTS"], "The Foundation",
                L1_ACCENT, L1_DARK)
    E.extend(divider(DIVIDER_X, L1_Y + 10, DIVIDER_X, L1_Y + L1_H - 10,
                     color="#86EFAC", stroke_width=1.5))

    # Spotlight banner
    E.extend(rect(CONTENT_X, L1_Y + 8, CONTENT_W, 32, fill=L1_ACCENT, stroke="none", rx=6))
    E.extend(text(CONTENT_X + 20, L1_Y + 24,
                  "Data Workbench builds this layer  —  "
                  "20+ agent skills, full lifecycle from source database to marketplace-published data product",
                  font_size=11, font_weight="700", color="#FFFFFF"))

    # Two product type columns
    half = (CONTENT_W - 12) // 2
    SA_X = CONTENT_X
    CF_X = CONTENT_X + half + 12

    # Source-aligned
    E.extend(rect(SA_X, L1_Y + 48, half, L1_H - 100,
                  fill="#DCFCE7", stroke="#86EFAC", stroke_width=1.5, rx=8))
    E.extend(header_band(SA_X, L1_Y + 48, half, 28,
                         label="SOURCE-ALIGNED  —  certify what you have",
                         fill="#16A34A", font_size=11, rx=8))
    sa_bullets = [
        "Discovered from existing databases and data warehouses",
        "Auto-profiled and AI-enriched with business descriptions",
        "ODCS data contract synthesized from approved graph state",
        "Quality rules generated from observed data patterns",
        "Published to marketplace with full schema lineage",
    ]
    for i, b in enumerate(sa_bullets):
        E.extend(text(SA_X + 20, L1_Y + 94 + i * 24, f"→  {b}",
                      font_size=10, color=L1_DARK))

    # Consumer-aligned
    E.extend(rect(CF_X, L1_Y + 48, half, L1_H - 100,
                  fill="#DCFCE7", stroke="#86EFAC", stroke_width=1.5, rx=8))
    E.extend(header_band(CF_X, L1_Y + 48, half, 28,
                         label="CONSUMER-ALIGNED  —  build what you need",
                         fill="#16A34A", font_size=11, rx=8))
    cf_bullets = [
        "Contract-first: schema designed before engineering begins",
        "Product Owner shapes columns, rules, and grain in a wizard",
        "Bound to source-aligned products via governed CONSUMES edges",
        "Virtual serving views generated with FK-aware join logic",
        "AI-ready: certified, governed, and semantically grounded",
    ]
    for i, b in enumerate(cf_bullets):
        E.extend(text(CF_X + 20, L1_Y + 94 + i * 24, f"→  {b}",
                      font_size=10, color=L1_DARK))

    # Data product characteristics row
    props_y = L1_Y + L1_H - 42
    props = ["Contracted (ODCS)", "Governed", "Quality-assured",
             "Lineage-traced", "Marketplace-discoverable"]
    pw = (CONTENT_W - (len(props) - 1) * 10) // len(props)
    for i, prop in enumerate(props):
        px = CONTENT_X + i * (pw + 10)
        E.extend(rect(px, props_y, pw, 28, fill="#BBF7D0", stroke="#4ADE80", rx=13))
        E.extend(text(px + pw // 2, props_y + 14, prop,
                      font_size=9, font_weight="700", color=L1_DARK,
                      text_anchor="middle"))

    # ── Gap indicator SRC→L1 (confined to 20px gap y=652-672) ───────────────
    E.extend(arrow(gap_cx, 668, gap_cx, 656, color="#475569", stroke_width=2))
    E.extend(text(gap_cx + 8, 662, "feeds", font_size=7, color=P["muted"]))

    # ════════════════════════════════════════════════════════════════════════
    # SOURCE SYSTEMS (bottom band)
    # ════════════════════════════════════════════════════════════════════════
    E.extend(rect(0, SRC_Y, W, SRC_H, fill="#1E293B", stroke="none", rx=0))
    src_items = ["Relational Databases", "Data Warehouses",
                 "Data Lakes", "Source APIs", "Legacy Systems"]
    src_w = (W - 80 - 4 * 16) // 5
    for i, item in enumerate(src_items):
        sx = 40 + i * (src_w + 16)
        E.extend(rect(sx, SRC_Y + 8, src_w, SRC_H - 16,
                      fill="#334155", stroke="#475569", rx=4))
        E.extend(text(sx + src_w // 2, SRC_Y + SRC_H // 2, item,
                      font_size=9, color="#94A3B8", text_anchor="middle"))

    # ── Footer ───────────────────────────────────────────────────────────────
    E.extend(rect(0, FOOTER_Y, W, H - FOOTER_Y, fill="#0F172A", stroke="none", rx=0))
    E.extend(text(W // 2, FOOTER_Y + 26,
                  "\"Algorithms are off the shelf, including GenAI. "
                  "Differentiation is in the readiness of the data for AI.\"",
                  font_size=13, font_weight="700", color="#F1F5F9",
                  text_anchor="middle"))
    E.extend(text(W // 2, FOOTER_Y + 50,
                  "Gartner  ·  D&A Leaders, Ready Your Data for AI  ·  2025",
                  font_size=9, color="#64748B", text_anchor="middle"))
    E.extend(divider(W // 2 - 300, FOOTER_Y + 64,
                     W // 2 + 300, FOOTER_Y + 64, color="#1E3A5F"))
    E.extend(text(W // 2, FOOTER_Y + 82,
                  "Layer 1. Built. Governed. Published.  "
                  "The foundation that makes the rest possible.",
                  font_size=11, font_weight="600", color="#94A3B8",
                  text_anchor="middle"))

    save(E, "arc3_data_product_stack.svg", width=W, height=H, bg="#F8FAFC")
    print("✓  arc3_data_product_stack.svg")


build()
