"""Server-side SVG renderer for the business-concept ontology diagram.

Replaces the Mermaid ``graph LR`` string that the Concepts tab used to render
client-side. The diagram makes the **two-layer model** explicit: a Domain /
concept layer (entity → attribute → value) on the left and a Data-product layer
(datasets + columns bound via ``:REPRESENTED_BY``) on the right, separated by a
dashed divider.

Two entry points:
  * ``build_concept_svg(settings, domain, include_data)`` — one domain.
  * ``build_all_domains_svg(settings, include_data)`` — every domain as a
    stacked lane, the ``shared`` domain in a central reference lane, with
    cross-domain ``:RELATES_TO`` edges and domain→domain ``:CONSUMES`` arrows
    (the consumer-aligned "amalgamation" story).

Pure stdlib — SVG strings are hand-rolled (mirrors how the old code hand-rolled
Mermaid); no SVG/layout library. Every label flows through ``_esc`` before it
is embedded because the frontend injects this output via
``dangerouslySetInnerHTML``.

Text width is estimated (``_CHAR_W``); a deliberately generous constant plus
``_ellipsize`` + a ``<title>`` carrying the full name keep labels from
overflowing their boxes. Layout is deterministic banded columns — readable for
~10-30 entities; pathological sizes scroll inside the frontend's ``overflow``
container.
"""

from __future__ import annotations

import html
from typing import Any, Optional

from . import business_concepts as concepts
from .models import AppSettings

# ── Geometry ─────────────────────────────────────────────────────────────────
_MARGIN = 28
_PAD_X = 12
_COL_GAP = 64
_DATA_SUBGAP = 28      # gap between dataset and column sub-columns in the data band
_ROW_H = 28
_ROW_GAP = 12
_GROUP_GAP = 22
_FONT = 13
_CHAR_W = 7.4          # generous px/char upper bound at 13px
_MIN_W = 92
_MAX_W = 230
_CHIP_H = 20
_LANE_PAD = 18
_LANE_GAP = 34
_GUTTER = 56           # left gutter (all-domains) for :CONSUMES arrows

# (fill, stroke, text) — reuses the original Mermaid classDef palette.
_PALETTE = {
    "entity": ("#dbeafe", "#1e40af", "#1e3a8a"),
    "attr": ("#eff6ff", "#60a5fa", "#1e40af"),
    "shared": ("#dcfce7", "#166534", "#14532d"),
    "data": ("#fef9c3", "#a16207", "#713f12"),
    "value": ("#faf5ff", "#7c3aed", "#5b21b6"),
}


def _esc(s: Any) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


def _text_w(s: str, font: int = _FONT) -> float:
    return _CHAR_W * len(s or "") * (font / _FONT)


def _node_w(label: str, min_w: int = _MIN_W, max_w: int = _MAX_W) -> float:
    return max(min_w, min(max_w, _text_w(label) + 2 * _PAD_X))


def _ellipsize(label: str, w: float, font: int = _FONT) -> str:
    label = label or ""
    avail = w - 2 * _PAD_X
    if _text_w(label, font) <= avail:
        return label
    max_chars = max(1, int(avail / (_CHAR_W * font / _FONT)) - 1)
    return label[:max_chars].rstrip() + "…"


class _Canvas:
    """Accumulates SVG fragments in z-order buckets and tracks node positions
    so edges can be wired by URI. Draw order: backgrounds → edges → nodes →
    relationship overlays (on top, semi-transparent)."""

    def __init__(self) -> None:
        self.bg: list[str] = []
        self.edges: list[str] = []
        self.nodes: list[str] = []
        self.rels: list[str] = []
        self.pos: dict[str, tuple[float, float, float, float, str]] = {}
        self.max_x = 0.0
        self.max_y = 0.0

    def _bump(self, x: float, y: float) -> None:
        self.max_x = max(self.max_x, x)
        self.max_y = max(self.max_y, y)

    def box(self, uri: str, x: float, y: float, w: float, h: float, cls: str,
            label: str, full: Optional[str] = None, shape: str = "rect") -> tuple:
        if uri in self.pos:
            return self.pos[uri]
        fill, stroke, color = _PALETTE[cls]
        disp = _ellipsize(label, w)
        rx = h / 2 if shape == "round" else 6
        title = _esc(full if full is not None else label)
        self.nodes.append(
            f'<g><title>{title}</title>'
            f'<rect x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" height="{h:.0f}" rx="{rx:.0f}" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>'
            f'<text x="{x + w / 2:.0f}" y="{y + h / 2 + 4:.0f}" text-anchor="middle" '
            f'font-size="{_FONT}" fill="{color}">{_esc(disp)}</text></g>'
        )
        self.pos[uri] = (x, y, w, h, cls)
        self._bump(x + w, y + h)
        return self.pos[uri]

    def chip(self, x: float, y: float, label: str) -> float:
        fill, stroke, color = _PALETTE["value"]
        w = max(38.0, _text_w(label, 11) + 16)
        self.nodes.append(
            f'<g><title>{_esc(label)}</title>'
            f'<rect x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" height="{_CHIP_H}" rx="{_CHIP_H / 2:.0f}" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="1"/>'
            f'<text x="{x + w / 2:.0f}" y="{y + _CHIP_H / 2 + 4:.0f}" text-anchor="middle" '
            f'font-size="11" fill="{color}">{_esc(_ellipsize(label, w, 11))}</text></g>'
        )
        self._bump(x + w, y + _CHIP_H)
        return w

    def elbow(self, x1: float, y1: float, x2: float, y2: float,
              dashed: bool = False, color: str = "#94a3b8", label: Optional[str] = None) -> None:
        midx = (x1 + x2) / 2
        dash = ' stroke-dasharray="4 3"' if dashed else ""
        self.edges.append(
            f'<path d="M{x1:.0f} {y1:.0f} H{midx:.0f} V{y2:.0f} H{x2:.0f}" '
            f'fill="none" stroke="{color}" stroke-width="1.3"{dash}/>'
        )
        if label:
            self._edge_label(midx, (y1 + y2) / 2, label, color, self.edges)

    def hline(self, x1: float, y1: float, x2: float, y2: float,
              color: str = "#a16207", dashed: bool = True, label: Optional[str] = None) -> None:
        dash = ' stroke-dasharray="5 3"' if dashed else ""
        self.edges.append(
            f'<line x1="{x1:.0f}" y1="{y1:.0f}" x2="{x2:.0f}" y2="{y2:.0f}" '
            f'stroke="{color}" stroke-width="1.2"{dash}/>'
        )
        if label:
            self._edge_label((x1 + x2) / 2, (y1 + y2) / 2, label, color, self.edges)

    def rel(self, r: dict) -> None:
        s = self.pos.get(r["from_uri"])
        t = self.pos.get(r["to_uri"])
        if not s or not t:
            return
        sx, sy = s[0] + s[2] / 2, s[1] + s[3] / 2
        tx, ty = t[0] + t[2] / 2, t[1] + t[3] / 2
        lbl = r.get("kind") or "related"
        if r.get("via_column"):
            lbl += f" · via {r['via_column']}"
        self.rels.append(
            f'<line x1="{sx:.0f}" y1="{sy:.0f}" x2="{tx:.0f}" y2="{ty:.0f}" '
            f'stroke="#64748b" stroke-width="1.3" stroke-opacity="0.55" marker-end="url(#arrow)"/>'
        )
        self._edge_label((sx + tx) / 2, (sy + ty) / 2, lbl, "#475569", self.rels)

    def _edge_label(self, cx: float, cy: float, label: str, color: str, bucket: list[str]) -> None:
        label = _ellipsize(label, 190, 11)
        w = _text_w(label, 11) + 8
        bucket.append(
            f'<rect x="{cx - w / 2:.0f}" y="{cy - 9:.0f}" width="{w:.0f}" height="16" rx="3" '
            f'fill="#ffffff" fill-opacity="0.85"/>'
            f'<text x="{cx:.0f}" y="{cy + 3:.0f}" text-anchor="middle" '
            f'font-size="11" fill="{color}">{_esc(label)}</text>'
        )


def _layer_label(x: float, y: float, text: str) -> str:
    return (f'<text x="{x:.0f}" y="{y:.0f}" font-size="11" font-weight="600" '
            f'fill="#94a3b8" letter-spacing="0.5">{_esc(text.upper())}</text>')


def _lane_rect(x: float, y: float, w: float, h: float, title: str, shared: bool = False) -> str:
    fill = "#f0fdf4" if shared else "#f8fafc"
    stroke = "#86efac" if shared else "#e2e8f0"
    return (
        f'<rect x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" height="{h:.0f}" rx="8" '
        f'fill="{fill}" stroke="{stroke}" stroke-width="1"/>'
        f'<text x="{x + 12:.0f}" y="{y + 19:.0f}" font-size="13" font-weight="700" '
        f'fill="#475569">{_esc(title)}</text>'
    )


def _layout_domain(cv: _Canvas, entities: list[dict], x0: float, y0: float,
                   include_data: bool) -> tuple[float, float, dict]:
    """Place one domain's entities (banded columns) starting at (x0, y0).
    Returns (width, height, meta) where meta carries column x-coordinates."""
    x_entity = x0
    ent_w = _MIN_W
    attr_w = _MIN_W
    chip_band = 0.0
    for e in entities:
        ent_w = max(ent_w, _node_w(e.get("name") or ""))
        for a in e.get("children") or []:
            if a.get("level") != "attribute":
                continue
            attr_w = max(attr_w, _node_w(a.get("name") or ""))
            cw = sum(max(38.0, _text_w(v.get("name") or v.get("value_token") or "", 11) + 16) + 6
                     for v in (a.get("children") or []))
            chip_band = max(chip_band, cw)

    x_attr = x_entity + ent_w + _COL_GAP
    attr_area_w = attr_w + (chip_band + 12 if chip_band else 0)

    # Data band = two sub-columns so an entity's centered dataset node never
    # collides with an attribute's column node sharing the same y.
    ds_w = 0.0
    col_w = 0.0
    if include_data:
        for e in entities:
            for d in e.get("represented_by_datasets") or []:
                ds_w = max(ds_w, _node_w("⊞ " + (d.get("physical_name") or "")))
            for a in e.get("children") or []:
                for b in a.get("represented_by") or []:
                    col_w = max(col_w, _node_w(b.get("column_name") or ""))
    x_ds = x_attr + attr_area_w + _COL_GAP
    x_col = x_ds + (ds_w + _DATA_SUBGAP if ds_w else 0)

    y = y0
    for e in entities:
        attrs = [a for a in (e.get("children") or []) if a.get("level") == "attribute"]
        rows: list[float] = []
        if attrs:
            for _ in attrs:
                rows.append(y)
                y += _ROW_H + _ROW_GAP
            ey = (rows[0] + rows[-1]) / 2
        else:
            ey = y
            y += _ROW_H + _ROW_GAP

        cv.box(e["uri"], x_entity, ey, ent_w, _ROW_H, "entity", e.get("name") or "")
        ecx, ecy = x_entity + ent_w, ey + _ROW_H / 2

        if include_data:
            ds = e.get("represented_by_datasets") or []
            if ds:
                first = ds[0]
                extra = f" (+{len(ds) - 1})" if len(ds) > 1 else ""
                full = "binds: " + ", ".join("⊞ " + (d.get("physical_name") or "") for d in ds)
                duri = first.get("dataset_uri") or (e["uri"] + ":ds")
                cv.box(duri, x_ds, ey, ds_w, _ROW_H, "data",
                       "⊞ " + (first.get("physical_name") or "") + extra, full=full, shape="round")
                cv.hline(ecx, ecy, x_ds, ey + _ROW_H / 2, label="binds")

        for a, ay in zip(attrs, rows):
            cv.box(a["uri"], x_attr, ay, attr_w, _ROW_H, "attr", a.get("name") or "")
            acy = ay + _ROW_H / 2
            cv.elbow(ecx, ecy, x_attr, acy, color="#3b82f6")

            cx = x_attr + attr_w + 8
            vals = a.get("children") or []
            for v in vals[:6]:
                cx += cv.chip(cx, ay + (_ROW_H - _CHIP_H) / 2,
                              v.get("name") or v.get("value_token") or "") + 6
            if len(vals) > 6:
                cv.chip(cx, ay + (_ROW_H - _CHIP_H) / 2, f"+{len(vals) - 6}")

            if include_data:
                cols = a.get("represented_by") or []
                if cols:
                    first = cols[0]
                    extra = f" (+{len(cols) - 1})" if len(cols) > 1 else ""
                    full = "columns: " + ", ".join(c.get("column_name") or "" for c in cols)
                    curi = first.get("column_uri") or (a["uri"] + ":col")
                    cv.box(curi, x_col, ay, col_w, _ROW_H, "data",
                           (first.get("column_name") or "") + extra, full=full, shape="round")
                    cv.hline(x_attr + attr_w, acy, x_col, ay + _ROW_H / 2)

        y += _GROUP_GAP

    if include_data:
        right = max(x_ds + ds_w, x_col + col_w)
    else:
        right = x_attr + attr_area_w
    return right - x0, y - y0, {"x_entity": x_entity, "x_attr": x_attr, "x_data": x_ds}


def _wrap(cv: _Canvas) -> str:
    w = cv.max_x + _MARGIN
    h = cv.max_y + _MARGIN
    defs = (
        '<defs>'
        '<marker id="arrow" markerWidth="9" markerHeight="9" refX="7" refY="3" '
        'orient="auto" markerUnits="userSpaceOnUse">'
        '<path d="M0,0 L7,3 L0,6 Z" fill="#64748b"/></marker>'
        '<marker id="arrowC" markerWidth="11" markerHeight="11" refX="8" refY="4" '
        'orient="auto" markerUnits="userSpaceOnUse">'
        '<path d="M0,0 L8,4 L0,8 Z" fill="#ea580c"/></marker>'
        '</defs>'
    )
    body = "".join(cv.bg) + "".join(cv.edges) + "".join(cv.nodes) + "".join(cv.rels)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.0f}" height="{h:.0f}" '
        f'viewBox="0 0 {w:.0f} {h:.0f}" font-family="system-ui,-apple-system,sans-serif">'
        f'{defs}<rect width="{w:.0f}" height="{h:.0f}" fill="#ffffff"/>{body}</svg>'
    )


def _empty_svg(msg: str) -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="340" height="80" viewBox="0 0 340 80" '
        'font-family="system-ui,sans-serif"><rect width="340" height="80" fill="#ffffff"/>'
        f'<text x="170" y="46" text-anchor="middle" font-size="14" fill="#94a3b8">{_esc(msg)}</text></svg>'
    )


def build_concept_svg(settings: AppSettings, domain: str, include_data: bool = True) -> str:
    """Render one domain's ontology as a layered SVG string."""
    tree = concepts.get_tree(settings, domain)
    entities = [e for e in tree if e.get("level") == "entity"]
    if not entities:
        return _empty_svg("No concepts in this domain yet")

    rels = concepts.list_relationships(settings, domain)
    shared_nodes = {e["uri"]: e for e in concepts.get_tree(settings, "shared")
                    if e.get("level") == "entity"}

    cv = _Canvas()
    top = _MARGIN + 22
    _, h, meta = _layout_domain(cv, entities, _MARGIN, top, include_data)

    # Two-layer affordances: column captions + a dashed divider.
    cv.bg.append(_layer_label(meta["x_entity"], _MARGIN + 10, "Domain / concept layer"))
    if include_data:
        div_x = meta["x_data"] - _COL_GAP / 2
        cv.bg.append(_layer_label(meta["x_data"], _MARGIN + 10, "Data-product layer"))
        cv.bg.append(
            f'<line x1="{div_x:.0f}" y1="{_MARGIN + 14:.0f}" x2="{div_x:.0f}" '
            f'y2="{top + h:.0f}" stroke="#e2e8f0" stroke-width="1.5" stroke-dasharray="3 4"/>'
        )

    # Relationship edges; shared targets get placed in a reference lane below.
    shared_x = _MARGIN
    shared_y = top + h + _LANE_GAP
    placed_shared = False
    for r in rels:
        if r.get("status") == "rejected":
            continue
        if r["from_uri"] not in cv.pos:
            continue
        tgt = r["to_uri"]
        if tgt not in cv.pos:
            sh = shared_nodes.get(tgt)
            if not sh:
                continue
            bw = _node_w(sh.get("name") or "")
            cv.box(tgt, shared_x, shared_y, bw, _ROW_H, "shared", sh.get("name") or "")
            shared_x += bw + 18
            placed_shared = True
        cv.rel(r)

    if placed_shared:
        cv.bg.append(_layer_label(_MARGIN, shared_y - 8, "Shared references"))

    return _wrap(cv)


def build_all_domains_svg(settings: AppSettings, include_data: bool = True) -> str:
    """Render every domain as a stacked lane, the ``shared`` domain as a central
    reference lane, with cross-domain :RELATES_TO edges and domain→domain
    :CONSUMES arrows."""
    all_tree = concepts.get_tree(settings, None)
    by_domain: dict[str, list[dict]] = {}
    for e in all_tree:
        if e.get("level") != "entity":
            continue
        by_domain.setdefault(e.get("domain") or "(none)", []).append(e)
    shared_entities = by_domain.pop("shared", [])

    if not by_domain and not shared_entities:
        return _empty_svg("No concepts yet — scaffold a domain first")

    cv = _Canvas()
    lane_x = _MARGIN + _GUTTER
    inner_x = lane_x + _LANE_PAD
    lane_meta: dict[str, tuple[float, float]] = {}

    y = _MARGIN
    for dom in sorted(by_domain.keys()):
        _, h, _ = _layout_domain(cv, by_domain[dom], inner_x, y + 30, include_data)
        lane_meta[dom] = (y, y + 30 + h + _LANE_PAD - _ROW_GAP)
        y = lane_meta[dom][1] + _LANE_GAP

    shared_top = y
    if shared_entities:
        sx = inner_x
        sy = y + 30
        for e in shared_entities:
            bw = _node_w(e.get("name") or "")
            cv.box(e["uri"], sx, sy, bw, _ROW_H, "shared", e.get("name") or "")
            sx += bw + 18
        y = sy + _ROW_H + _LANE_PAD
    shared_bot = y

    # Lane backgrounds (drawn first). Width spans all placed nodes.
    lane_w = cv.max_x + _LANE_PAD - lane_x
    for dom, (lt, lb) in lane_meta.items():
        cv.bg.append(_lane_rect(lane_x, lt, lane_w, lb - lt, dom))
    if shared_entities:
        cv.bg.append(_lane_rect(lane_x, shared_top, lane_w, shared_bot - shared_top,
                                "shared — cross-domain references", shared=True))

    # Cross-domain :RELATES_TO (only edges whose both endpoints we placed).
    for dom in sorted(by_domain.keys()):
        for r in concepts.list_relationships(settings, dom):
            if r.get("status") == "rejected":
                continue
            if r["from_uri"] in cv.pos and r["to_uri"] in cv.pos:
                cv.rel(r)

    # :CONSUMES overlay — domain→domain arrows in the left gutter.
    pair_products: dict[tuple[str, str], list[str]] = {}
    for c in concepts.list_cross_domain_consumes(settings):
        cd, sd = c.get("consumer_domain"), c.get("source_domain")
        if cd in lane_meta and sd in lane_meta:
            pair_products.setdefault((cd, sd), []).append(c.get("product_name") or "")
    for i, ((cd, sd), prods) in enumerate(sorted(pair_products.items())):
        y_c = sum(lane_meta[cd]) / 2
        y_s = sum(lane_meta[sd]) / 2
        ctrl_x = _MARGIN + 6 + (i % 4) * 9
        names = ", ".join(p for p in prods if p) or f"{len(prods)} product(s)"
        cv.rels.append(
            f'<path d="M{lane_x:.0f} {y_c:.0f} Q{ctrl_x:.0f} {(y_c + y_s) / 2:.0f} '
            f'{lane_x:.0f} {y_s:.0f}" fill="none" stroke="#ea580c" stroke-width="2" '
            f'stroke-dasharray="7 4" stroke-opacity="0.85" marker-end="url(#arrowC)">'
            f'<title>consumes: {_esc(names)}</title></path>'
        )
        cv.rels.append(
            f'<text x="{ctrl_x:.0f}" y="{(y_c + y_s) / 2 - 3:.0f}" text-anchor="middle" '
            f'font-size="10" font-weight="600" fill="#ea580c">consumes</text>'
        )

    return _wrap(cv)
