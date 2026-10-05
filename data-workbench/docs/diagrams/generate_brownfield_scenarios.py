#!/usr/bin/env python3
"""Brownfield scenarios decision-tree diagram.

Walks a Data Product Owner through which path to take when bringing in a
pre-existing data product. Highlights what's implemented today vs. the gap
around missing source-aligned dependencies, plus the open question about
the corpus that should drive missing-source recommendations.
"""

import sys
sys.path.insert(0, "/home/niel/.claude/skills/excalidraw-draw/scripts")

from excalidraw_lib import rect, arrow, text, bind, save


# ── Colors ─────────────────────────────────────────────────────────────────
GREEN_BG = "#dcfce7"
GREEN_S = "#16a34a"
GREEN_TXT = "#15803d"

RED_BG = "#fee2e2"
RED_S = "#dc2626"
RED_TXT = "#b91c1c"

BLUE_BG = "#dbeafe"
BLUE_S = "#2563eb"
BLUE_TXT = "#1d4ed8"

AMBER_BG = "#fef3c7"
AMBER_S = "#d97706"
AMBER_TXT = "#92400e"

ROOT_BG = "#e0f2fe"
ROOT_S = "#0369a1"

SLATE = "#1e293b"
SLATE_DIM = "#64748b"
MONO_FAM = 3   # Cascadia
SANS_FAM = 2   # Helvetica


def diagram():
    E = []
    S = {}

    # ── Header ─────────────────────────────────────────────────────────────
    _, e = text(40, 30,
                "Brownfield Scenarios in the Data Workbench",
                font_size=30, color="#0f172a", font_family=SANS_FAM)
    E.extend(e)
    _, e = text(40, 76,
                "Decision tree for bringing pre-existing data products in. "
                "Green = implemented; red = gap; blue = open question.",
                font_size=14, color=SLATE_DIM, font_family=SANS_FAM)
    E.extend(e)

    # ── Root node ──────────────────────────────────────────────────────────
    S["root"], e = rect(460, 130, 600, 80,
                        bg=ROOT_BG, stroke=ROOT_S, stroke_width=3, roundness=8)
    E.extend(e)
    _, e = text(490, 155, "PO wants to bring in a brownfield",
                font_size=20, color="#0c4a6e", font_family=SANS_FAM)
    E.extend(e)
    _, e = text(490, 180, "data product",
                font_size=20, color="#0c4a6e", font_family=SANS_FAM)
    E.extend(e)

    # ── Scenario boxes ─────────────────────────────────────────────────────
    # Column layout: S1 center=280, S2 center=760, S3 center=1240
    SCEN_Y, SCEN_H = 320, 340
    SCEN_W = 400

    def scenario_box(name, x, y, w, h, num, title, body_lines, ok=True):
        if ok:
            bg, stroke, stroke_w, badge_color = GREEN_BG, GREEN_S, 3, GREEN_TXT
        else:
            bg, stroke, stroke_w, badge_color = RED_BG, RED_S, 3, RED_TXT
        S[name], e = rect(x, y, w, h,
                          bg=bg, stroke=stroke, stroke_width=stroke_w, roundness=8)
        E.extend(e)
        # Eyebrow label (SCENARIO N)
        _, e = text(x + 16, y + 14,
                    "SCENARIO " + str(num),
                    font_size=12, color=badge_color, font_family=SANS_FAM)
        E.extend(e)
        # Title
        _, e = text(x + 16, y + 36, title,
                    font_size=18, color=SLATE, font_family=SANS_FAM)
        E.extend(e)
        # Body
        _, e = text(x + 16, y + 80, "\n".join(body_lines),
                    font_size=13, color=SLATE, font_family=SANS_FAM)
        E.extend(e)
        # Status pill at bottom of box
        status_txt = ("✓ CAPABILITY EXISTS" if ok else "⚠ GAP — needs new workflow")
        _, e = text(x + 16, y + h - 32, status_txt,
                    font_size=14, color=badge_color, font_family=SANS_FAM)
        E.extend(e)
        return S[name]

    # SCENARIO 1
    scenario_box(
        "s1", 80, SCEN_Y, SCEN_W, SCEN_H, 1,
        "Ingest existing ODCS",
        [
            "Route:     /product/ingest",
            "Page:      IngestExistingProductPage",
            "Archetype: dpe-sa",
            "",
            "Default workflows:",
            "  • odcs_to_dprod",
            "  • lineage_discovery",
            "  • marketplace",
            "",
            "ProductRequest kind = 'ingest'",
        ],
        ok=True,
    )
    _, e = text(80, 670,
                "workbench/backend/routers/\n"
                "  ingest_products.py:164-200",
                font_size=11, color=SLATE_DIM, font_family=MONO_FAM)
    E.extend(e)

    # SCENARIO 2
    scenario_box(
        "s2", 560, SCEN_Y, SCEN_W, SCEN_H, 2,
        "Discover source from raw DB",
        [
            "Route:     /product/new/source",
            "Page:      NewSourceProductWizard",
            "Archetype: dpe-sa",
            "",
            "Default workflows:",
            "  • data_discovery",
            "  • metadata_enrichment",
            "  • source_naming_recommendations",
            "  • mark_discovery_complete",
            "  • po_source_validation",
            "  • product_materialization_sa",
        ],
        ok=True,
    )
    _, e = text(560, 670,
                "workbench/backend/archetypes.py\n"
                "  (dpe-sa default template)",
                font_size=11, color=SLATE_DIM, font_family=MONO_FAM)
    E.extend(e)

    # SCENARIO 3
    scenario_box(
        "s3", 1040, SCEN_Y, SCEN_W, SCEN_H, 3,
        "Consumer-aligned product",
        [
            "Route:     /product/new/consumer",
            "Page:      NewProductWizard (8 steps)",
            "Archetype: dpe-cf",
            "",
            "Step 2 'Source Inputs' fetches:",
            "  GET /api/marketplace",
            "       ?product_kind=source",
            "       &domain=X",
            "",
            "PO multi-selects → ODCS inputs[]",
            "→ MERGE :CONSUMES edges on save",
        ],
        ok=True,
    )
    _, e = text(1040, 670,
                "workbench/frontend/src/pages/product/\n"
                "  NewProductWizard.tsx",
                font_size=11, color=SLATE_DIM, font_family=MONO_FAM)
    E.extend(e)

    # ── Arrows: root → each scenario (with question label) ────────────────
    # Root bottom-center (760, 210). Scenario top-centers: (280, 320),
    # (760, 320), (1240, 320). Use elbowed paths so labels land cleanly.
    aid, ae = arrow(S["root"], S["s1"],
                    [(760, 210), (760, 265), (280, 265), (280, 320)],
                    stroke=SLATE, stroke_width=2,
                    label="Have an ODCS YAML spec?",
                    label_font_size=12)
    E.extend(ae)
    bind(E, aid, S["root"], S["s1"])

    aid, ae = arrow(S["root"], S["s2"],
                    [(760, 210), (760, 320)],
                    stroke=SLATE, stroke_width=2,
                    label="Have raw source DB, no spec?",
                    label_font_size=12)
    E.extend(ae)
    bind(E, aid, S["root"], S["s2"])

    aid, ae = arrow(S["root"], S["s3"],
                    [(760, 210), (760, 265), (1240, 265), (1240, 320)],
                    stroke=SLATE, stroke_width=2,
                    label="Compose a consumer-aligned product?",
                    label_font_size=12)
    E.extend(ae)
    bind(E, aid, S["root"], S["s3"])

    # ── Sub-decision under Scenario 3 ──────────────────────────────────────
    SUB_X, SUB_Y, SUB_W, SUB_H = 1040, 750, 400, 80
    S["sub"], e = rect(SUB_X, SUB_Y, SUB_W, SUB_H,
                       bg="#f1f5f9", stroke="#475569", stroke_width=2,
                       roundness=8)
    E.extend(e)
    _, e = text(SUB_X + 16, SUB_Y + 14,
                "Q: Do all required source products already",
                font_size=14, color=SLATE, font_family=SANS_FAM)
    E.extend(e)
    _, e = text(SUB_X + 16, SUB_Y + 40,
                "    exist in the workbench?",
                font_size=14, color=SLATE, font_family=SANS_FAM)
    E.extend(e)

    # Arrow from S3 → sub-decision
    aid, ae = arrow(S["s3"], S["sub"],
                    [(1240, 660), (1240, 750)],
                    stroke=SLATE, stroke_width=2)
    E.extend(ae)
    bind(E, aid, S["s3"], S["sub"])

    # ── 3a and 3b boxes ────────────────────────────────────────────────────
    # 3a (YES): left under sub
    S["s3a"], e = rect(840, 900, 300, 320,
                       bg=GREEN_BG, stroke=GREEN_S, stroke_width=3, roundness=8)
    E.extend(e)
    _, e = text(856, 914, "PATH 3a",
                font_size=12, color=GREEN_TXT, font_family=SANS_FAM)
    E.extend(e)
    _, e = text(856, 936, "All sources exist",
                font_size=18, color=SLATE, font_family=SANS_FAM)
    E.extend(e)
    _, e = text(856, 976,
                "• MERGE :CONSUMES edges in\n"
                "  _save_odcs_to_graph\n"
                "\n"
                "• Engineer runs data_mapping\n"
                "  with --source-mode dprod\n"
                "\n"
                "• Mappings written as\n"
                "  :ColumnMapping →\n"
                "  :DProdColumn",
                font_size=13, color=SLATE, font_family=SANS_FAM)
    E.extend(e)
    _, e = text(856, 1188, "✓ CAPABILITY EXISTS",
                font_size=14, color=GREEN_TXT, font_family=SANS_FAM)
    E.extend(e)

    # 3b (NO): right under sub, larger box (more text)
    S["s3b"], e = rect(1180, 900, 400, 400,
                       bg=RED_BG, stroke=RED_S, stroke_width=3, roundness=8)
    E.extend(e)
    _, e = text(1196, 914, "PATH 3b",
                font_size=12, color=RED_TXT, font_family=SANS_FAM)
    E.extend(e)
    _, e = text(1196, 936, "⚠ Missing source product",
                font_size=18, color=SLATE, font_family=SANS_FAM)
    E.extend(e)
    _, e = text(1196, 980,
                "No 'request a source product to\n"
                "be created' workflow today.\n"
                "\n"
                "• discovery-advisor surfaces\n"
                "  similar products + ODCS\n"
                "  templates, but can't kick off\n"
                "  a new dpe-sa run.\n"
                "\n"
                "• ProductRequestKind.\n"
                "  source_rediscovery exists,\n"
                "  but only for adding tables to\n"
                "  an ALREADY-DEPLOYED source —\n"
                "  not for creating one.",
                font_size=13, color=SLATE, font_family=SANS_FAM)
    E.extend(e)
    _, e = text(1196, 1268, "⚠ GAP — needs new workflow",
                font_size=14, color=RED_TXT, font_family=SANS_FAM)
    E.extend(e)

    # YES / NO arrows
    aid, ae = arrow(S["sub"], S["s3a"],
                    [(1240, 830), (1240, 870), (990, 870), (990, 900)],
                    stroke=GREEN_S, stroke_width=2,
                    label="YES — all sources registered",
                    label_font_size=12)
    E.extend(ae)
    bind(E, aid, S["sub"], S["s3a"])

    aid, ae = arrow(S["sub"], S["s3b"],
                    [(1240, 830), (1240, 870), (1380, 870), (1380, 900)],
                    stroke=RED_S, stroke_width=2,
                    label="NO — missing source",
                    label_font_size=12)
    E.extend(ae)
    bind(E, aid, S["sub"], S["s3b"])

    # ── CX example callout (dashed, anchored alongside Scenario 3) ────────
    S["cx"], e = rect(1620, 320, 320, 340,
                      bg=AMBER_BG, stroke=AMBER_S, stroke_width=2,
                      stroke_style="dashed", roundness=8)
    E.extend(e)
    _, e = text(1636, 334, "EXAMPLE  ·  Marketing & CX",
                font_size=12, color=AMBER_TXT, font_family=SANS_FAM)
    E.extend(e)
    _, e = text(1636, 358, "Customer 360 product",
                font_size=18, color=SLATE, font_family=SANS_FAM)
    E.extend(e)
    _, e = text(1636, 398,
                "One row per customer with:\n"
                "  • current segment\n"
                "  • lifetime spend\n"
                "  • order count\n"
                "  • return flag\n"
                "\n"
                "Composes:\n"
                "  customer table\n"
                "  + SCD-2 segment\n"
                "  + orders\n"
                "\n"
                "→ Hits 3a if all three source\n"
                "  products exist.\n"
                "→ Hits 3b if 'customer segment'\n"
                "  isn't registered yet.",
                font_size=12, color=SLATE, font_family=SANS_FAM)
    E.extend(e)

    # Dashed connector from S3 to CX (informational, not flow)
    aid, ae = arrow(S["s3"], S["cx"],
                    [(1440, 480), (1620, 480)],
                    stroke=AMBER_S, stroke_width=1,
                    stroke_style="dashed",
                    label="illustrative",
                    label_font_size=10)
    E.extend(ae)
    bind(E, aid, S["s3"], S["cx"])

    # ── Open question callout (bottom, blue dashed, full width) ───────────
    OQ_X, OQ_Y, OQ_W, OQ_H = 80, 1360, 1860, 200
    S["oq"], e = rect(OQ_X, OQ_Y, OQ_W, OQ_H,
                      bg=BLUE_BG, stroke=BLUE_S, stroke_width=2,
                      stroke_style="dashed", roundness=8)
    E.extend(e)
    _, e = text(OQ_X + 20, OQ_Y + 16,
                "OPEN QUESTION  ·  Corpus for missing-source recommendations",
                font_size=16, color=BLUE_TXT, font_family=SANS_FAM)
    E.extend(e)
    _, e = text(OQ_X + 20, OQ_Y + 50,
                "What corpus drives recommendations when a needed source product DOESN'T exist yet?",
                font_size=14, color=SLATE, font_family=SANS_FAM)
    E.extend(e)
    _, e = text(OQ_X + 20, OQ_Y + 84,
                "Today (template-level only):",
                font_size=13, color=SLATE, font_family=SANS_FAM)
    E.extend(e)
    _, e = text(OQ_X + 40, OQ_Y + 108,
                "•  playbook/domain_catalogs/{domain}.yaml    — starter columns per domain\n"
                "•  playbook/odcs_templates/                              — reusable ODCS specs\n"
                "•  playbook/transformation_catalogs/                   — mapping recipes",
                font_size=12, color=SLATE, font_family=MONO_FAM)
    E.extend(e)
    _, e = text(OQ_X + 20, OQ_Y + 168,
                "Missing: a live registry of actual data products available across the org "
                "that we could ingest to seed brownfield discovery.",
                font_size=13, color=BLUE_TXT, font_family=SANS_FAM)
    E.extend(e)

    save(E, "/home/niel/working/claudecodedash/docs/diagrams/brownfield-scenarios.excalidraw")


if __name__ == "__main__":
    diagram()
