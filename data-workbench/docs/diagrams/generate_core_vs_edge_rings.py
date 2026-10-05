#!/usr/bin/env python3
"""Core vs. Edge rings — where metadata lives relative to the Data Workbench graph.

Three concentric zones (core ontology / owned extensions / referenced-at-edge)
with four edge 'gateways' pointing at the external platforms that own the detail.
Companion visual for research/2026-07-10-core-vs-edge-onepager.md (Scott discussion).
"""

import math
import sys

sys.path.insert(0, "/home/niel/.claude/skills/svg-architect/scripts")

from svg_lib import *  # noqa: E402,F403

CX, CY = 560, 505
R_CORE, R_OWNED, R_EDGE = 120, 275, 385


def circle(cx, cy, r, fill, stroke, stroke_width=1.5, dashed=False):
    dash = ' stroke-dasharray="7,5"' if dashed else ""
    return [
        f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{fill}" '
        f'stroke="{stroke}" stroke-width="{stroke_width}"{dash} />'
    ]


def on_ring(radius, angle_deg):
    """Point on a ring around (CX, CY); angle 0 = east, counter-clockwise."""
    a = math.radians(angle_deg)
    return CX + radius * math.cos(a), CY - radius * math.sin(a)


def diagram():
    E = []

    E.extend(title_bar(0, 0, 1600, 55,
                       title="Core vs. Edge — Where Metadata Lives",
                       subtitle="Data Workbench ontology boundary · reference what you observe, model what you execute · 2026-07-10"))

    # ── Rings (paint outermost first) ─────────────────────────────────────
    E.extend(circle(CX, CY, R_EDGE, fill=P["harness_fill"], stroke=P["harness_stroke"],
                    stroke_width=2, dashed=True))
    E.extend(circle(CX, CY, R_OWNED, fill=P["skills_fill"], stroke=P["skills_stroke"],
                    stroke_width=3))
    E.extend(circle(CX, CY, R_CORE, fill=P["data_fill"], stroke=P["data_stroke"],
                    stroke_width=2))

    # ── Ring labels ───────────────────────────────────────────────────────
    E.extend(text(CX, 150, "REFERENCED AT EDGE", font_size=14, font_weight="700",
                  color="#B45309", text_anchor="middle"))
    E.extend(text(CX, 168, "thin stubs + URIs · standards spoken outward",
                  font_size=10, color=P["muted"], text_anchor="middle"))

    E.extend(text(CX, 268, "OWNED EXTENSIONS", font_size=14, font_weight="700",
                  color="#15803D", text_anchor="middle"))
    E.extend(text(CX, 285, "we compile, validate, serve it — never core",
                  font_size=10, color=P["muted"], text_anchor="middle"))

    # ── Core disc content ─────────────────────────────────────────────────
    E.extend(text(CX, 438, "CORE ONTOLOGY", font_size=14, font_weight="700",
                  color="#6D28D9", text_anchor="middle"))
    E.extend(text(CX, 454, "shared T-box · single architect", font_size=9.5,
                  color=P["muted"], text_anchor="middle"))
    core_lines = [
        "DCAT catalog · dataset · column",
        "DPROD data product",
        "Business semantics (SKOS)",
        "PROV-O provenance + HITL review",
        "SHACL rules we author + execute",
        "Contract identity + versioning",
        "Derivation skeleton (lineage)",
    ]
    for i, ln in enumerate(core_lines):
        E.extend(text(CX, 476 + i * 16, ln, font_size=11, color=P["title"],
                      text_anchor="middle"))

    # ── Owned-extension boxes on the middle ring ──────────────────────────
    owned = [
        (45,  170, "Transform DSL", "compiler input → views + dbt"),
        (135, 170, "Dataset shape", ":DatasetTransform · SCD · joins"),
        (180, 140, "Semantic runtime", "embeddings · Q&A"),
        (225, 170, "Scoring · playbooks", "OSI · QA · remediation"),
        (270, 170, "Embedded engines", "dbt · GX · Pandera (inside!)"),
        (315, 170, "Serving emitters", "view DDL · dbt scaffold"),
    ]
    for angle, w, label, sub in owned:
        x, y = on_ring(192, angle)
        E.extend(rect(x - w / 2, y - 23, w, 46, label=f"{label}\n{sub}",
                      fill=P["box_fill"], stroke=P["skills_stroke"],
                      stroke_width=1.5, font_size=10, font_weight="600"))

    # ── Edge gateways (in the amber band, right arc, pointing outward) ────
    gateways = [
        ("LINEAGE EDGE", "OpenLineage RunEvents", 735, 215),
        ("QUALITY EDGE", "ruleSource='external'", 900, 395),
        ("CONTRACT EDGE", "ODCS file ↔ thin stub", 900, 615),
        ("POLICY EDGE", "PolicyControl + URI", 735, 795),
    ]
    gw_pos = {}
    for label, sub, x, y in gateways:
        E.extend(rect(x - 80, y - 23, 160, 46, label=f"{label}\n{sub}",
                      fill="#FFF7E6", stroke=P["harness_stroke"],
                      stroke_width=2, font_size=10.5, font_weight="700"))
        gw_pos[label] = (x + 80, y)

    # ── External platform cards (outside the model) ───────────────────────
    E.extend(text(1090, 108, "OUTSIDE THE MODEL — external systems of record",
                  font_size=12, font_weight="700", color=P["body"]))

    def card(y, h, header, lines):
        out = []
        out.extend(layer_container(1090, y, 470, h,
                                   fill=P["box_fill"], stroke=P["ui_stroke"],
                                   stroke_width=1.5, header_fill=P["ui_stroke"],
                                   header_h=28, header_label=header))
        for i, ln in enumerate(lines):
            out.extend(text(1106, y + 48 + i * 18, ln, font_size=11, color=P["body"]))
        return out

    E.extend(card(120, 122, "Catalogs & lineage consumers", [
        "AWS DataZone / SageMaker · MS Fabric / Purview",
        "DataHub · OpenMetadata (Collate) · Atlan",
        "IBM Manta / watsonx — all ingest OpenLineage:",
        "one emitter reaches every one of them",
    ]))
    E.extend(card(262, 104, "Quality systems of record", [
        "Informatica CDQ (Salesforce, 11/2025) · Soda",
        "client GX suites (GX Cloud → FICO)",
    ]))
    E.extend(card(386, 104, "Contract & product standards", [
        "Bitol ODCS v3.1 files (strict schema, exec. SLAs)",
        "ODPS · datacontract-cli",
    ]))
    E.extend(card(510, 122, "Policy / GRC", [
        "Archer (Evolv, AI governance) · ServiceNow IRM",
        "OneTrust · Collibra policies",
        "EU AI Act high-risk enforcement: Aug 2026",
    ]))

    # ── Arrows: gateways → external cards ─────────────────────────────────
    lx, ly = gw_pos["LINEAGE EDGE"]
    E.extend(arrow(lx, ly, 1088, 181, color=P["arrow_strong"], stroke_width=2,
                   label="we emit"))
    qx, qy = gw_pos["QUALITY EDGE"]
    E.extend(arrow(qx, qy, 1088, 314, dashed=True))
    E.extend(text(1020, 344, "stub + URI", font_size=9, color=P["muted"],
                  text_anchor="middle"))
    cx2, cy2 = gw_pos["CONTRACT EDGE"]
    E.extend(arrow(cx2, cy2, 1088, 438, dashed=True, bidir=True))
    E.extend(text(1010, 545, "ingest ↔ export", font_size=9, color=P["muted"],
                  text_anchor="middle"))
    px, py = gw_pos["POLICY EDGE"]
    E.extend(arrow(px, py, 1088, 571, dashed=True, label="tag + point"))

    # ── The boundary test ─────────────────────────────────────────────────
    E.extend(annotation_box(1090, 668, 470, 130,
                            title="The boundary test",
                            lines_=[
                                "“Reference what you observe; model what you execute.”",
                                "Copy of another system's data → stub + URI (edge)",
                                "Read by our engines → model it (owned extension)",
                                "Needed by other teams too → promote to core",
                                "Round-trip-only / leaf blob → document, not model",
                            ]))

    save(E, "core_vs_edge_rings.svg", width=1600, height=960)


if __name__ == "__main__":
    diagram()
