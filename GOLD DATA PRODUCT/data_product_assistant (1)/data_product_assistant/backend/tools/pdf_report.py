"""
pdf_report.py — Generates a formatted PDF summary of a RequirementsOutput dict.

Usage:
    from tools.pdf_report import generate_requirements_pdf
    pdf_bytes = generate_requirements_pdf(requirements_dict)
"""

import io
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

# ── Colour palette ─────────────────────────────────────────────────────────────
_GREEN   = colors.HexColor("#A100FF")   # Accenture purple
_DARK    = colors.HexColor("#1A1A2E")
_GREY    = colors.HexColor("#6B7280")
_LIGHT   = colors.HexColor("#F3F4F6")
_WHITE   = colors.white
_RED     = colors.HexColor("#DC2626")

PAGE_W, PAGE_H = A4
MARGIN = 18 * mm


def _styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "title",
            fontName="Helvetica-Bold",
            fontSize=18,
            textColor=_DARK,
            spaceAfter=2,
        ),
        "subtitle": ParagraphStyle(
            "subtitle",
            fontName="Helvetica",
            fontSize=9,
            textColor=_GREY,
            spaceAfter=10,
        ),
        "section": ParagraphStyle(
            "section",
            fontName="Helvetica-Bold",
            fontSize=11,
            textColor=_GREEN,
            spaceBefore=12,
            spaceAfter=4,
        ),
        "body": ParagraphStyle(
            "body",
            fontName="Helvetica",
            fontSize=9,
            textColor=_DARK,
            leading=13,
        ),
        "label": ParagraphStyle(
            "label",
            fontName="Helvetica-Bold",
            fontSize=9,
            textColor=_DARK,
        ),
        "tag": ParagraphStyle(
            "tag",
            fontName="Helvetica",
            fontSize=8,
            textColor=_GREY,
        ),
        "footer": ParagraphStyle(
            "footer",
            fontName="Helvetica",
            fontSize=7,
            textColor=_GREY,
        ),
        "status_ok": ParagraphStyle(
            "status_ok",
            fontName="Helvetica-Bold",
            fontSize=9,
            textColor=_GREEN,
        ),
        "status_no": ParagraphStyle(
            "status_no",
            fontName="Helvetica-Bold",
            fontSize=9,
            textColor=_RED,
        ),
    }


def _val(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, list):
        return ", ".join(str(i) for i in v) if v else "—"
    return str(v).strip() or "—"


def _overview_table(data: dict, styles: dict) -> Table:
    rows = [
        [Paragraph("Field", styles["label"]), Paragraph("Value", styles["label"])],
        ["Use Case Name",  _val(data.get("use_case_name"))],
        ["Domain",         _val(data.get("domain"))],
        ["Consumer Role",  _val(data.get("consumer_role"))],
        ["Data Freshness", _val(data.get("data_freshness"))],
        ["Classification", _val(data.get("classification_hint"))],
    ]
    col_w = [(PAGE_W - 2 * MARGIN) * p for p in (0.35, 0.65)]
    tbl = Table(rows, colWidths=col_w)
    tbl.setStyle(TableStyle([
        ("BACKGROUND",  (0, 0), (-1, 0), _GREEN),
        ("TEXTCOLOR",   (0, 0), (-1, 0), _WHITE),
        ("FONTNAME",    (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",    (0, 0), (-1, -1), 9),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [_WHITE, _LIGHT]),
        ("GRID",        (0, 0), (-1, -1), 0.4, colors.HexColor("#E5E7EB")),
        ("LEFTPADDING",  (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING",   (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 5),
        ("VALIGN",      (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return tbl


def _data_points_table(data_points: list, styles: dict) -> Table:
    rows = [
        [
            Paragraph("Name", styles["label"]),
            Paragraph("Kind", styles["label"]),
            Paragraph("Derived", styles["label"]),
            Paragraph("Description", styles["label"]),
        ]
    ]
    for dp in data_points:
        if not isinstance(dp, dict):
            continue
        kind = dp.get("kind", "")
        derived = ""
        if kind == "kpi":
            derived = "Yes" if dp.get("is_derived") else "No"
        rows.append([
            Paragraph(_val(dp.get("name")), styles["body"]),
            Paragraph(kind.upper() if kind else "—", styles["tag"]),
            Paragraph(derived or "—", styles["tag"]),
            Paragraph(_val(dp.get("description")), styles["body"]),
        ])
    total_w = PAGE_W - 2 * MARGIN
    col_w = [total_w * p for p in (0.22, 0.10, 0.10, 0.58)]
    tbl = Table(rows, colWidths=col_w)
    tbl.setStyle(TableStyle([
        ("BACKGROUND",   (0, 0), (-1, 0), _GREEN),
        ("TEXTCOLOR",    (0, 0), (-1, 0), _WHITE),
        ("FONTNAME",     (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",     (0, 0), (-1, -1), 9),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [_WHITE, _LIGHT]),
        ("GRID",         (0, 0), (-1, -1), 0.4, colors.HexColor("#E5E7EB")),
        ("LEFTPADDING",  (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING",   (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 5),
        ("VALIGN",       (0, 0), (-1, -1), "TOP"),
    ]))
    return tbl


def _bullet_list(items: list, styles: dict) -> list:
    """Return a list of Paragraph flowables for a simple bullet list."""
    if not items:
        return [Paragraph("None specified", styles["body"])]
    return [Paragraph(f"• {item}", styles["body"]) for item in items]


def generate_requirements_pdf(data: dict) -> bytes:
    """
    Render a RequirementsOutput dict into a formatted PDF.
    Returns the raw PDF bytes.
    """
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=MARGIN,
        bottomMargin=MARGIN,
        title="Data Product Requirements Summary",
    )
    styles = _styles()
    story = []

    # ── Header ────────────────────────────────────────────────────────────────
    story.append(Paragraph("Data Product Requirements Summary", styles["title"]))
    story.append(HRFlowable(
        width="100%", thickness=1.5, color=_GREEN,
        spaceBefore=10, spaceAfter=14,
    ))

    # ── Overview ──────────────────────────────────────────────────────────────
    story.append(Paragraph("Overview", styles["section"]))
    story.append(_overview_table(data, styles))
    story.append(Spacer(1, 8))

    # ── Data Points / Attributes ──────────────────────────────────────────────
    story.append(Paragraph("Data Points &amp; Attributes", styles["section"]))
    data_points = data.get("data_points") or data.get("kpis") or []
    if data_points:
        story.append(_data_points_table(data_points, styles))
    else:
        story.append(Paragraph("None specified", styles["body"]))
    story.append(Spacer(1, 8))

    # ── Granularity ───────────────────────────────────────────────────────────
    story.append(Paragraph("Dimensions / Granularity", styles["section"]))
    granularity = data.get("granularity") or []
    if granularity:
        items = []
        for g in granularity:
            if isinstance(g, dict):
                dim = g.get("dimension", "")
                tag = " (inferred)" if not g.get("confirmed_by_user") else ""
                items.append(f"{dim}{tag}")
            else:
                items.append(str(g))
        story.extend(_bullet_list(items, styles))
    else:
        story.append(Paragraph("None specified", styles["body"]))
    story.append(Spacer(1, 8))

    # ── Filters ───────────────────────────────────────────────────────────────
    story.append(Paragraph("Filters", styles["section"]))
    filters = data.get("filters") or []
    if filters:
        items = []
        for f in filters:
            if isinstance(f, dict):
                items.append(
                    f"{f.get('field', '?')}  {f.get('operator', '?')}  {f.get('value', '?')}"
                )
            else:
                items.append(str(f))
        story.extend(_bullet_list(items, styles))
    else:
        story.append(Paragraph("None specified", styles["body"]))
    story.append(Spacer(1, 8))

    # ── Data Sources ──────────────────────────────────────────────────────────
    story.append(Paragraph("Data Sources", styles["section"]))
    sources = data.get("data_sources") or []
    if sources:
        items = []
        for s in sources:
            if isinstance(s, dict):
                name = s.get("source_name", "")
                notes = s.get("notes", "")
                items.append(f"{name}  —  {notes}" if notes else name)
            else:
                items.append(str(s))
        story.extend(_bullet_list(items, styles))
    else:
        story.append(Paragraph("None specified", styles["body"]))
    story.append(Spacer(1, 8))

    # ── Use Case Signals ──────────────────────────────────────────────────────
    signals = data.get("use_case_signals") or []
    if signals:
        story.append(Paragraph("Use Case Signals", styles["section"]))
        story.extend(_bullet_list([str(s) for s in signals], styles))
        story.append(Spacer(1, 8))

    # ── Handoff Status ────────────────────────────────────────────────────────
    story.append(HRFlowable(width="100%", thickness=0.5, color=_GREY, spaceBefore=6, spaceAfter=6))
    handoff = data.get("handoff_ready", False)
    status_style = styles["status_ok"] if handoff else styles["status_no"]
    status_text  = "Ready for handoff: Yes" if handoff else "Ready for handoff: No — mandatory fields missing"
    story.append(Paragraph(status_text, status_style))

    field_status = data.get("field_status", {})
    gaps = field_status.get("needs_clarification", [])
    if gaps:
        story.append(Paragraph(f"Gaps flagged: {', '.join(gaps)}", styles["body"]))
    unknown = field_status.get("unknown_per_user", [])
    if unknown:
        story.append(Paragraph(f"Marked unknown by user: {', '.join(unknown)}", styles["body"]))

    # ── Footer note ───────────────────────────────────────────────────────────
    story.append(Spacer(1, 12))
    story.append(Paragraph(
        "This document is auto-generated by the Requirements Agent. "
        "It reflects the structured output at the point of user confirmation.",
        styles["footer"],
    ))

    doc.build(story)
    return buf.getvalue()


# ── Shared helpers for the agent-specific PDFs ────────────────────────────────

def _new_doc(buf, title: str) -> SimpleDocTemplate:
    return SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN, bottomMargin=MARGIN,
        title=title,
    )


def _header(story: list, styles: dict, title: str, subtitle_suffix: str = "") -> None:
    story.append(Paragraph(title, styles["title"]))
    if subtitle_suffix:
        story.append(Paragraph(subtitle_suffix, styles["subtitle"]))
    story.append(HRFlowable(
        width="100%", thickness=1.5, color=_GREEN,
        spaceBefore=10, spaceAfter=14,
    ))


def _kv_table(rows: list, styles: dict) -> Table:
    """Two-column header→value table used by overview sections."""
    body = [[Paragraph("Field", styles["label"]), Paragraph("Value", styles["label"])]]
    body.extend([[label, _val(value)] for label, value in rows])
    col_w = [(PAGE_W - 2 * MARGIN) * p for p in (0.35, 0.65)]
    tbl = Table(body, colWidths=col_w)
    tbl.setStyle(TableStyle([
        ("BACKGROUND",     (0, 0), (-1, 0), _GREEN),
        ("TEXTCOLOR",      (0, 0), (-1, 0), _WHITE),
        ("FONTNAME",       (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",       (0, 0), (-1, -1), 9),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [_WHITE, _LIGHT]),
        ("GRID",           (0, 0), (-1, -1), 0.4, colors.HexColor("#E5E7EB")),
        ("LEFTPADDING",    (0, 0), (-1, -1), 6),
        ("RIGHTPADDING",   (0, 0), (-1, -1), 6),
        ("TOPPADDING",     (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING",  (0, 0), (-1, -1), 5),
        ("VALIGN",         (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return tbl


def _grid_table(headers: list, rows: list, col_pct: list, styles: dict) -> Table:
    body = [[Paragraph(h, styles["label"]) for h in headers]]
    for row in rows:
        body.append([Paragraph(_val(c), styles["body"]) for c in row])
    total_w = PAGE_W - 2 * MARGIN
    col_w = [total_w * p for p in col_pct]
    tbl = Table(body, colWidths=col_w, repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND",     (0, 0), (-1, 0), _GREEN),
        ("TEXTCOLOR",      (0, 0), (-1, 0), _WHITE),
        ("FONTNAME",       (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",       (0, 0), (-1, -1), 9),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [_WHITE, _LIGHT]),
        ("GRID",           (0, 0), (-1, -1), 0.4, colors.HexColor("#E5E7EB")),
        ("LEFTPADDING",    (0, 0), (-1, -1), 6),
        ("RIGHTPADDING",   (0, 0), (-1, -1), 6),
        ("TOPPADDING",     (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING",  (0, 0), (-1, -1), 5),
        ("VALIGN",         (0, 0), (-1, -1), "TOP"),
    ]))
    return tbl


def _footer_note(story: list, styles: dict, agent_name: str) -> None:
    story.append(Spacer(1, 12))
    story.append(Paragraph(
        f"This document is auto-generated by the {agent_name}. "
        "It reflects the structured output at the point of user approval.",
        styles["footer"],
    ))


# ── Classification PDF ────────────────────────────────────────────────────────

def generate_classification_pdf(data: dict) -> bytes:
    buf = io.BytesIO()
    doc = _new_doc(buf, "Use Case Classification")
    styles = _styles()
    story: list = []

    _header(story, styles, "Use Case Classification")

    confidence = data.get("confidence")
    conf_str = f"{float(confidence):.2f}" if isinstance(confidence, (int, float)) else _val(confidence)

    story.append(Paragraph("Overview", styles["section"]))
    story.append(_kv_table([
        ("Use Case Type",         data.get("use_case_type")),
        ("Schema Design Pattern", data.get("schema_design_pattern")),
        ("Confidence",            conf_str),
        ("Confirmed by user",     "Yes" if data.get("confirmed_by_user") else "No"),
    ], styles))
    story.append(Spacer(1, 8))

    rationale = data.get("rationale") or data.get("reasoning")
    if rationale:
        story.append(Paragraph("Rationale", styles["section"]))
        story.append(Paragraph(_val(rationale), styles["body"]))
        story.append(Spacer(1, 8))

    signals = data.get("signals_matched") or data.get("matched_signals") or []
    if signals:
        story.append(Paragraph("Signals Matched", styles["section"]))
        items = [str(s) if not isinstance(s, dict) else s.get("signal") or s.get("name") or str(s) for s in signals]
        story.extend(_bullet_list(items, styles))
        story.append(Spacer(1, 8))

    _footer_note(story, styles, "Use Case Classification Agent")
    doc.build(story)
    return buf.getvalue()


# ── Discovery PDF ─────────────────────────────────────────────────────────────

# Mirrors what the UI's DiscoveryResultCard renders. Accepts the structured
# `discovery_view` payload built by agents.discovery._build_discovery_view —
# falls back to building a minimal view from the raw match lists if a caller
# still passes the unwrapped discovery result.

def _layer_status_value(summary: dict) -> str:
    status = (summary or {}).get("status", "not_found")
    count = (summary or {}).get("table_count", 0)
    if status == "found":
        return f"Found — {count} reusable table{'s' if count != 1 else ''}"
    return "Not found"


def _summary_tiles(layer_summary: dict, styles: dict) -> Table:
    """3 side-by-side Gold/Silver/Bronze tiles that mirror the UI summary row.

    Each tile is its own single-column 3-row table (label / badge / body) so the
    paragraphs stack vertically. The outer 1×3 table places the three tiles
    side-by-side and gets the visible borders.
    """
    def _tile_table(label: str, key: str, inner_w: float) -> Table:
        info = layer_summary.get(key) or {}
        status = info.get("status", "not_found")
        count = info.get("table_count", 0)
        if status == "found":
            badge_text  = "Found"
            badge_color = "#A100FF"
            body_text = f"{count} reusable table{'s' if count != 1 else ''} found."
        else:
            badge_text  = "Not found"
            badge_color = "#DC2626"
            if label == "Gold":
                body_text = "No matching consumption table was found."
            elif label == "Bronze":
                body_text = "No source-layer table was returned for this search."
            else:
                body_text = "No matching table was found."

        tile = Table(
            [
                [Paragraph(f"<b>{label}</b>", styles["label"])],
                [Paragraph(f'<font color="{badge_color}"><b>{badge_text}</b></font>', styles["body"])],
                [Paragraph(body_text, styles["body"])],
            ],
            colWidths=[inner_w],
        )
        tile.setStyle(TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING",(0, 0), (-1, -1), 0),
            ("TOPPADDING",  (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING",(0, 0), (-1, -1), 3),
            ("VALIGN",      (0, 0), (-1, -1), "TOP"),
        ]))
        return tile

    total_w = PAGE_W - 2 * MARGIN
    col_w = total_w / 3
    # Inner content width = tile column width minus the outer LEFTPADDING+RIGHTPADDING (8+8).
    inner_w = col_w - 16

    outer = Table(
        [[
            _tile_table("Gold",   "gold",   inner_w),
            _tile_table("Silver", "silver", inner_w),
            _tile_table("Bronze", "bronze", inner_w),
        ]],
        colWidths=[col_w, col_w, col_w],
    )
    outer.setStyle(TableStyle([
        ("BOX",         (0, 0), (-1, -1), 0.5, colors.HexColor("#E5E7EB")),
        ("INNERGRID",   (0, 0), (-1, -1), 0.5, colors.HexColor("#E5E7EB")),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING",(0, 0), (-1, -1), 8),
        ("TOPPADDING",  (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 8),
        ("VALIGN",      (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND",  (0, 0), (-1, -1), _LIGHT),
    ]))
    return outer


def _visual_flow_layer(label: str, summary: dict, tables: list, styles: dict) -> Table:
    """One row of the visual discovery map — layer label on the left, table
    cards on the right. All column widths are computed against the *available*
    width inside each cell, never against the full page, so nothing overflows.
    """
    info = summary or {}
    status = info.get("status", "not_found")
    found = status == "found"

    badge_color = "#A100FF" if found else "#DC2626"
    if found:
        n = len(tables) if tables else 0
        badge_text = f"Found: {n} table{'s' if n != 1 else ''}"
    else:
        badge_text = "Not found"

    # ── width math ────────────────────────────────────────────────────────────
    row_w     = PAGE_W - 2 * MARGIN
    LEFT_FRAC = 0.22
    LEFT_W    = row_w * LEFT_FRAC
    RIGHT_W   = row_w * (1 - LEFT_FRAC)
    OUTER_PAD = 8
    # Width inside the right cell after accounting for that cell's own padding
    right_inner_w = RIGHT_W - 2 * OUTER_PAD

    # ── left cell ─────────────────────────────────────────────────────────────
    left_cell = [
        Paragraph(f"<b>{label}</b>", styles["label"]),
        Spacer(1, 4),
        Paragraph(f'<font color="{badge_color}"><b>{badge_text}</b></font>', styles["body"]),
    ]

    # ── right cell: stacked table cards ───────────────────────────────────────
    if found and tables:
        # Each card is a single-column inner table containing:
        #   row 1: table name header
        #   row 2: mappings sub-table (2 columns, fits inside the card)
        card_inner_w = right_inner_w - 16   # subtract card's own L/R padding (8 each)
        mapping_col1 = card_inner_w * 0.50
        mapping_col2 = card_inner_w - mapping_col1

        card_blocks = []
        for t in tables:
            if not isinstance(t, dict):
                continue
            tname = t.get("table_short_name", "")
            mapping_rows = []
            for r in (t.get("rows") or []):
                if not isinstance(r, dict):
                    continue
                mapping_rows.append([
                    Paragraph(_val(r.get("data_point")), styles["body"]),
                    Paragraph(
                        f'<font name="Courier">{_val(r.get("matched_column_or_logic"))}</font>',
                        styles["body"],
                    ),
                ])

            mapping_table = None
            if mapping_rows:
                mapping_table = Table(mapping_rows, colWidths=[mapping_col1, mapping_col2])
                mapping_table.setStyle(TableStyle([
                    ("FONTSIZE",     (0, 0), (-1, -1), 8.5),
                    ("LEFTPADDING",  (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING",   (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING",(0, 0), (-1, -1), 3),
                    ("VALIGN",       (0, 0), (-1, -1), "TOP"),
                ]))

            card_rows = [
                [Paragraph(f'<font color="#A100FF"><b>{tname}</b></font>', styles["body"])],
            ]
            if mapping_table is not None:
                card_rows.append([mapping_table])

            card = Table(card_rows, colWidths=[card_inner_w])
            card.setStyle(TableStyle([
                ("BOX",         (0, 0), (-1, -1), 0.5, colors.HexColor("#E5E7EB")),
                ("BACKGROUND",  (0, 0), (-1, -1), colors.HexColor("#F0FDF4")),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING",(0, 0), (-1, -1), 8),
                ("TOPPADDING",  (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING",(0, 0), (-1, -1), 6),
                ("VALIGN",      (0, 0), (-1, -1), "TOP"),
            ]))
            card_blocks.append([card])

        # Stack cards vertically inside the right cell
        right_cell = Table(card_blocks, colWidths=[right_inner_w]) if card_blocks else Paragraph("—", styles["body"])
        if card_blocks:
            right_cell.setStyle(TableStyle([
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING",(0, 0), (-1, -1), 0),
                ("TOPPADDING",  (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING",(0, 0), (-1, -1), 4),
                ("VALIGN",      (0, 0), (-1, -1), "TOP"),
            ]))
    else:
        empty_msg = (
            "No matching consumption table was found." if label == "Gold" else
            "No source-layer table was returned for this search." if label == "Bronze" else
            "No matching table was found."
        )
        right_cell = Paragraph(empty_msg, styles["body"])

    # ── outer layer row ───────────────────────────────────────────────────────
    layer_row = Table(
        [[left_cell, right_cell]],
        colWidths=[LEFT_W, RIGHT_W],
    )
    border_color = _GREEN if found else _RED
    layer_row.setStyle(TableStyle([
        ("LINEBEFORE", (0, 0), (0, 0), 3, border_color),
        ("BACKGROUND", (0, 0), (0, 0), _LIGHT),
        ("LEFTPADDING", (0, 0), (-1, -1), OUTER_PAD),
        ("RIGHTPADDING",(0, 0), (-1, -1), OUTER_PAD),
        ("TOPPADDING",  (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 10),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return layer_row


def _esc(v) -> str:
    """XML-escape dynamic text before embedding it in a reportlab Paragraph
    (derivation formulas can contain <, >, &)."""
    return _val(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_VERDICT_PDF = {
    "reuse":     ("Reuse",     "#16A34A"),
    "extend":    ("Extend",    "#D97706"),
    "build_new": ("Build New", "#2563EB"),
}
_LAYER_BAR = {"gold": "#B8860B", "silver": "#64748B", "bronze": "#A16207"}


def _disc_layer_block(story: list, entry: dict, styles: dict) -> None:
    """One merged layer section: coloured bar, a 'Data Points Found' table,
    verdict-badged table cards (+ what build-new tables deliver), and — for
    Bronze — the 'Source data not found' callout."""
    lk = entry.get("layer", "")
    label = entry.get("label") or lk.capitalize()
    tables = entry.get("tables") or []

    bar = Table([[Paragraph(f'<font color="#FFFFFF"><b>{_esc(label)}</b></font>', styles["body"])]],
                colWidths=[PAGE_W - 2 * MARGIN])
    bar.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(_LAYER_BAR.get(lk, "#64748B"))),
        ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(bar)
    story.append(Spacer(1, 4))

    # Data Points Found (aggregate supplies across this layer's tables)
    found, seen = [], set()
    for t in tables:
        for s in (t.get("supplies") or []):
            dp = s.get("data_point")
            if dp and dp not in seen:
                seen.add(dp)
                found.append(s)
    if found:
        story.append(Paragraph("<b>Data Points Found</b>", styles["body"]))
        rows = [[s.get("data_point", ""), s.get("column_or_logic") or "—", s.get("sample") or "—"]
                for s in found]
        story.append(_grid_table(["Data point", "Matched column / logic", "Sample (from catalog)"],
                                 rows, [0.34, 0.33, 0.33], styles))
        story.append(Spacer(1, 4))

    for t in tables:
        if not isinstance(t, dict):
            continue
        vlabel, vcolor = _VERDICT_PDF.get(t.get("verdict", "reuse"), ("Reuse", "#16A34A"))
        head = f'<font color="{vcolor}"><b>[{vlabel}]</b></font>  <font name="Courier">{_esc(t.get("name"))}</font>'
        if t.get("proposed"):
            head += '  <font color="#2563EB">(proposed)</font>'
        story.append(Paragraph(head, styles["body"]))
        if t.get("description"):
            story.append(Paragraph(f"<b>Table Description:</b> {_esc(t['description'])}", styles["body"]))
        if t.get("derived_from"):
            story.append(Paragraph(f'<b>Table Derived From:</b> <font name="Courier">{_esc(t["derived_from"])}</font>', styles["body"]))
        if t.get("rationale"):
            story.append(Paragraph(f'<font color="#6B7280"><i>{_esc(t["rationale"])}</i></font>', styles["body"]))
        delivers = t.get("delivers") or []
        if delivers:
            dl = ("This table conforms the below data points (feed the Gold table):"
                  if (lk == "silver" and t.get("proposed")) else "This table delivers the below data points:")
            story.append(Paragraph(f"<b>{dl}</b>", styles["body"]))
            for d in delivers:
                txt = f"&bull; <b>{_esc(d.get('data_point'))}</b>"
                if d.get("derivation"):
                    txt += f" — {_esc(d['derivation'])}"
                story.append(Paragraph(txt, styles["body"]))
        story.append(Spacer(1, 6))

    if lk == "bronze":
        missing = entry.get("missing_from_source") or []
        if missing:
            story.append(Paragraph(
                '<font color="#92400E"><b>Source data not found in Bronze — must be sourced upstream (cannot be created):</b></font>',
                styles["body"]))
            for m in missing:
                txt = f"&bull; <b>{_esc(m.get('data_point'))}</b>"
                if m.get("needs"):
                    txt += f' — needs <font name="Courier">{_esc(m["needs"])}</font>'
                if m.get("note"):
                    txt += f" — {_esc(m['note'])}"
                story.append(Paragraph(txt, styles["body"]))
            story.append(Spacer(1, 6))
    story.append(Spacer(1, 4))


def _disc_lineage(story: list, lineage: dict, styles: dict) -> None:
    """Cascade chain (Bronze → Silver → Gold) + per-data-point flow markers."""
    nodes = lineage.get("nodes") or []
    flows = lineage.get("flows") or []
    by: dict = {"bronze": [], "silver": [], "gold": []}
    for n in nodes:
        by.setdefault(n.get("layer"), []).append(n)

    for idx, lk in enumerate(["bronze", "silver", "gold"]):
        chips = by.get(lk) or []
        if chips:
            parts = []
            for n in chips:
                color = _VERDICT_PDF.get(n.get("verdict", "reuse"), ("", "#16A34A"))[1]
                nm = _esc(n.get("name")) + (" (new)" if n.get("proposed") else "")
                parts.append(f'<font color="{color}"><b>{nm}</b></font>')
            line = f"<b>{lk.capitalize()}:</b> " + " , ".join(parts)
        else:
            line = f'<b>{lk.capitalize()}:</b> <font color="#9CA3AF">—</font>'
        story.append(Paragraph(line, styles["body"]))
        if idx < 2:
            story.append(Paragraph('<font color="#6B7280">↓</font>', styles["body"]))
    story.append(Spacer(1, 6))

    for f in flows:
        if f.get("status") == "deliverable":
            txt = (f'<font color="#166534"><b>FOUND</b></font> <b>{_esc(f.get("data_point"))}</b> — '
                   f'{(_val(f.get("origin_layer")) or "").capitalize()}: '
                   f'<font name="Courier">{_esc(f.get("origin_column"))}</font>')
        else:
            txt = f'<font color="#B42318"><b>GAP</b></font> <b>{_esc(f.get("data_point"))}</b> — not available in source'
        story.append(Paragraph(txt, styles["body"]))


def generate_discovery_pdf(data: dict) -> bytes:
    # Accept either the structured discovery_view or the raw discovery result.
    view = data.get("discovery_view") if isinstance(data, dict) and "discovery_view" in data else data
    view = view or {}

    buf = io.BytesIO()
    doc = _new_doc(buf, "Data Discovery Results")
    styles = _styles()
    story: list = []

    _header(story, styles, "Data Discovery Results")

    if view.get("use_case"):
        story.append(Paragraph(f'<b>For Use Case:</b> {_esc(view["use_case"])}', styles["body"]))
        story.append(Spacer(1, 8))

    layers_plan = view.get("layers_plan") or []
    flows = (view.get("lineage") or {}).get("flows") or []

    # Headline
    if view.get("headline"):
        hl = Table([[Paragraph(_esc(view["headline"]), styles["body"])]], colWidths=[PAGE_W - 2 * MARGIN])
        hl.setStyle(TableStyle([
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#E5E7EB")),
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
            ("LEFTPADDING", (0, 0), (-1, -1), 12), ("RIGHTPADDING", (0, 0), (-1, -1), 12),
            ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ]))
        story.append(hl)
        story.append(Spacer(1, 8))

    # Data points searched
    chips = view.get("search_criteria") or []
    if chips:
        story.append(Paragraph("Data points searched", styles["section"]))
        story.append(Paragraph(", ".join(_esc(c) for c in chips), styles["body"]))
        story.append(Spacer(1, 8))

    # Data point status (found vs not found)
    if flows:
        story.append(Paragraph("Data Point Status", styles["section"]))
        rows = []
        for f in flows:
            if f.get("status") == "deliverable":
                rows.append([f.get("data_point", ""), "Found", (_val(f.get("origin_layer")) or "—").capitalize()])
            else:
                rows.append([f.get("data_point", ""), "Not found", "—"])
        story.append(_grid_table(["Data Point", "Status", "Layer"], rows, [0.5, 0.25, 0.25], styles))
        story.append(Spacer(1, 8))

    # Summary by Layer (merged sections)
    if layers_plan:
        story.append(Paragraph("Summary by Layer", styles["section"]))
        for entry in layers_plan:
            if isinstance(entry, dict):
                _disc_layer_block(story, entry, styles)

        story.append(Paragraph("Lineage / Cascade", styles["section"]))
        _disc_lineage(story, view.get("lineage") or {}, styles)
    else:
        # Legacy fallback (old discovery_view without layers_plan).
        story.append(Paragraph("Summary", styles["section"]))
        story.append(_summary_tiles(view.get("layer_summary") or {}, styles))
        if view.get("result_text"):
            story.append(Spacer(1, 8))
            story.append(Paragraph(_esc(view["result_text"]), styles["body"]))

    _footer_note(story, styles, "Discovery Agent")
    doc.build(story)
    return buf.getvalue()


# ── Challenger PDF ────────────────────────────────────────────────────────────

_VERDICT_LABEL = {"clean": "CLEAN", "concerns": "CONCERNS", "blockers": "BLOCKERS"}


def generate_challenger_pdf(data: dict) -> bytes:
    buf = io.BytesIO()
    doc = _new_doc(buf, "Challenger Review")
    styles = _styles()
    story: list = []

    verdict = (data.get("verdict") or "").lower()
    verdict_label = _VERDICT_LABEL.get(verdict, "—")
    _header(story, styles, "Challenger Review", f"Verdict: {verdict_label}")

    checks = data.get("checks") or []
    if checks:
        story.append(Paragraph("Consistency Checks", styles["section"]))
        rows = [
            [c.get("label", ""), "PASS" if c.get("passed") else "FAIL", _val(c.get("note") or c.get("detail"))]
            for c in checks if isinstance(c, dict)
        ]
        story.append(_grid_table(
            ["Check", "Result", "Notes"],
            rows, [0.40, 0.12, 0.48], styles,
        ))
        story.append(Spacer(1, 8))

    if data.get("summary"):
        story.append(Paragraph("Summary", styles["section"]))
        story.append(Paragraph(_val(data["summary"]), styles["body"]))
        story.append(Spacer(1, 8))

    dq = data.get("design_queue") or {}
    curated = dq.get("curated") or []
    enriched = dq.get("enriched") or []
    if curated or enriched:
        story.append(Paragraph("Design Queue", styles["section"]))
        if curated:
            story.append(Paragraph("<b>Curated (Bronze/Silver — design first):</b>", styles["body"]))
            story.extend(_bullet_list([str(x) for x in curated], styles))
            story.append(Spacer(1, 4))
        if enriched:
            story.append(Paragraph("<b>Enriched (Gold — design second):</b>", styles["body"]))
            story.extend(_bullet_list([str(x) for x in enriched], styles))

    _footer_note(story, styles, "Challenger Agent")
    doc.build(story)
    return buf.getvalue()


# ── Data Product PDF ──────────────────────────────────────────────────────────

def generate_data_product_pdf(dp: dict) -> bytes:
    buf = io.BytesIO()
    doc = _new_doc(buf, "Data Product Definition")
    styles = _styles()
    story: list = []

    _header(story, styles, f"Data Product: {dp.get('name', '—')}")

    story.append(Paragraph("Overview", styles["section"]))
    story.append(_kv_table([
        ("Data Product ID", dp.get("data_product_id")),
        ("Domain",          dp.get("domain")),
        ("Use Case Type",   dp.get("use_case_type")),
        ("Status",          dp.get("status")),
        ("Version",         dp.get("version")),
        ("Created",         dp.get("created_at")),
    ], styles))
    story.append(Spacer(1, 8))

    kpis = dp.get("kpis") or []
    if kpis:
        story.append(Paragraph("KPIs", styles["section"]))
        rows = []
        for k in kpis:
            src = (k.get("source_columns") or [])
            tbl = src[0]["table"].split(".")[-1] if src else "—"
            cols = ", ".join(src[0].get("columns", [])[:3]) if src else "—"
            rows.append([k.get("name", ""), k.get("status", ""), tbl, cols])
        story.append(_grid_table(
            ["KPI", "Status", "Source Table", "Key Columns"],
            rows, [0.28, 0.18, 0.24, 0.30], styles,
        ))
        story.append(Spacer(1, 8))

    dims = dp.get("dimensions") or []
    if dims:
        story.append(Paragraph("Dimensions", styles["section"]))
        rows = []
        for d in dims:
            src = d.get("source") or {}
            tbl = src.get("table", "—").split(".")[-1] if src.get("table") else "—"
            col = src.get("column") or (f"add {d.get('suggested_new_column')}" if d.get("suggested_new_column") else "—")
            rows.append([d.get("name", ""), d.get("status", ""), tbl, col])
        story.append(_grid_table(
            ["Dimension", "Status", "Source Table", "Column"],
            rows, [0.30, 0.18, 0.27, 0.25], styles,
        ))
        story.append(Spacer(1, 8))

    rec = dp.get("recommended_tables") or {}
    rec_rows = []
    for layer in ("gold", "silver", "bronze"):
        r = rec.get(layer)
        if r:
            rec_rows.append([layer.capitalize(), r.get("table", ""), (r.get("decision") or "").upper(), f"{r.get('score', 0):.2f}"])
    if rec_rows:
        story.append(Paragraph("Recommended Tables", styles["section"]))
        story.append(_grid_table(
            ["Layer", "Table", "Decision", "Score"],
            rec_rows, [0.15, 0.45, 0.20, 0.20], styles,
        ))
        story.append(Spacer(1, 8))

    actions = dp.get("build_actions") or []
    if actions:
        story.append(Paragraph("Build Actions", styles["section"]))
        items = []
        for a in actions:
            cols = ", ".join(a.get("add_columns", []))
            items.append(f"Extend {a.get('table', '?')} — add columns: {cols}")
        story.extend(_bullet_list(items, styles))
        story.append(Spacer(1, 8))

    gaps = dp.get("gaps") or []
    if gaps:
        story.append(Paragraph("Gaps", styles["section"]))
        items = []
        for g in gaps:
            if g.get("type") == "missing_kpi":
                hint = f" — hint: {g['computation_hint']}" if g.get("computation_hint") else ""
                items.append(f"{g.get('name', '?')} (KPI): no source columns found{hint}")
            else:
                items.append(f"{g.get('name', '?')} (dimension): not found in any matched table")
        story.extend(_bullet_list(items, styles))
        story.append(Spacer(1, 8))

    _footer_note(story, styles, "Data Product Generator")
    doc.build(story)
    return buf.getvalue()
