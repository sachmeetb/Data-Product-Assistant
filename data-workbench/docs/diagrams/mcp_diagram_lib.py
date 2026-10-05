#!/usr/bin/env python3
"""Small SVG helpers for the MCP architecture diagrams."""

from __future__ import annotations

from html import escape
from pathlib import Path

W = 1440
H = 810
FONT = "Inter, Segoe UI, Helvetica Neue, Arial, sans-serif"
MONO = "JetBrains Mono, SFMono-Regular, Consolas, monospace"


def _attrs(**attrs: object) -> str:
    parts: list[str] = []
    for key, value in attrs.items():
        if value is None:
            continue
        if key.endswith("_"):
            key = key[:-1]
        key = key.replace("_", "-")
        parts.append(f'{key}="{escape(str(value), quote=True)}"')
    return " ".join(parts)


def tag(name: str, content: str = "", **attrs: object) -> str:
    attr = _attrs(**attrs)
    if content:
        return f"  <{name} {attr}>{content}</{name}>" if attr else f"  <{name}>{content}</{name}>"
    return f"  <{name} {attr}/>" if attr else f"  <{name}/>"


def rect(
    x: float,
    y: float,
    w: float,
    h: float,
    *,
    fill: str,
    stroke: str | None = None,
    stroke_width: float | None = None,
    rx: float = 8,
    opacity: float | None = None,
    filter_: str | None = None,
    dash: str | None = None,
) -> str:
    return tag(
        "rect",
        x=x,
        y=y,
        width=w,
        height=h,
        rx=rx,
        fill=fill,
        stroke=stroke,
        stroke_width=stroke_width,
        opacity=opacity,
        filter=filter_,
        stroke_dasharray=dash,
    )


def circle(
    cx: float,
    cy: float,
    r: float,
    *,
    fill: str,
    stroke: str | None = None,
    stroke_width: float | None = None,
    opacity: float | None = None,
    filter_: str | None = None,
) -> str:
    return tag(
        "circle",
        cx=cx,
        cy=cy,
        r=r,
        fill=fill,
        stroke=stroke,
        stroke_width=stroke_width,
        opacity=opacity,
        filter=filter_,
    )


def line(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    *,
    stroke: str,
    stroke_width: float = 2,
    marker: str | None = None,
    opacity: float | None = None,
    dash: str | None = None,
) -> str:
    return tag(
        "line",
        x1=x1,
        y1=y1,
        x2=x2,
        y2=y2,
        stroke=stroke,
        stroke_width=stroke_width,
        stroke_linecap="round",
        marker_end=f"url(#{marker})" if marker else None,
        opacity=opacity,
        stroke_dasharray=dash,
    )


def path(
    d: str,
    *,
    fill: str = "none",
    stroke: str | None = None,
    stroke_width: float | None = None,
    marker: str | None = None,
    opacity: float | None = None,
    dash: str | None = None,
    filter_: str | None = None,
) -> str:
    return tag(
        "path",
        d=d,
        fill=fill,
        stroke=stroke,
        stroke_width=stroke_width,
        stroke_linecap="round",
        stroke_linejoin="round",
        marker_end=f"url(#{marker})" if marker else None,
        opacity=opacity,
        stroke_dasharray=dash,
        filter=filter_,
    )


def polygon(
    points: str,
    *,
    fill: str,
    stroke: str | None = None,
    stroke_width: float | None = None,
    opacity: float | None = None,
    filter_: str | None = None,
) -> str:
    return tag(
        "polygon",
        points=points,
        fill=fill,
        stroke=stroke,
        stroke_width=stroke_width,
        opacity=opacity,
        filter=filter_,
    )


def text(
    x: float,
    y: float,
    label: object,
    *,
    size: float = 12,
    fill: str = "#0F172A",
    weight: int | str = 500,
    anchor: str = "start",
    family: str = FONT,
    opacity: float | None = None,
    baseline: str = "middle",
    style: str | None = None,
) -> str:
    return tag(
        "text",
        escape(str(label)),
        x=x,
        y=y,
        font_size=size,
        font_weight=weight,
        fill=fill,
        text_anchor=anchor,
        font_family=family,
        opacity=opacity,
        dominant_baseline=baseline,
        style=style,
    )


def multiline(
    x: float,
    y: float,
    lines: list[str],
    *,
    size: float = 12,
    fill: str = "#334155",
    weight: int | str = 500,
    anchor: str = "start",
    line_height: float = 16,
    family: str = FONT,
    opacity: float | None = None,
) -> list[str]:
    return [
        text(
            x,
            y + i * line_height,
            line_text,
            size=size,
            fill=fill,
            weight=weight,
            anchor=anchor,
            family=family,
            opacity=opacity,
        )
        for i, line_text in enumerate(lines)
    ]


def pill(
    x: float,
    y: float,
    w: float,
    h: float,
    label: str,
    *,
    fill: str,
    stroke: str,
    text_fill: str,
    size: float = 11,
    weight: int | str = 700,
    family: str = FONT,
) -> list[str]:
    return [
        rect(x, y, w, h, fill=fill, stroke=stroke, stroke_width=1, rx=h / 2),
        text(x + w / 2, y + h / 2 + 0.5, label, size=size, fill=text_fill, weight=weight, anchor="middle", family=family),
    ]


def arrow_marker(marker_id: str, color: str) -> str:
    return (
        f'    <marker id="{marker_id}" viewBox="0 0 10 10" refX="8.5" refY="5" '
        'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{color}"/></marker>'
    )


def base_defs() -> str:
    return "\n".join(
        [
            "  <defs>",
            '    <linearGradient id="page-bg" x1="0" x2="1" y1="0" y2="1">',
            '      <stop offset="0" stop-color="#F8FAFC"/>',
            '      <stop offset="0.52" stop-color="#F4F7FB"/>',
            '      <stop offset="1" stop-color="#EEF4F7"/>',
            "    </linearGradient>",
            '    <linearGradient id="header-bg" x1="0" x2="1" y1="0" y2="0">',
            '      <stop offset="0" stop-color="#0F172A"/>',
            '      <stop offset="0.45" stop-color="#172033"/>',
            '      <stop offset="1" stop-color="#14213D"/>',
            "    </linearGradient>",
            '    <linearGradient id="endpoint-bg" x1="0" x2="1" y1="0" y2="1">',
            '      <stop offset="0" stop-color="#132238"/>',
            '      <stop offset="1" stop-color="#0F172A"/>',
            "    </linearGradient>",
            '    <linearGradient id="agent-bg" x1="0" x2="1" y1="0" y2="1">',
            '      <stop offset="0" stop-color="#FFF7ED"/>',
            '      <stop offset="1" stop-color="#FEF3C7"/>',
            "    </linearGradient>",
            '    <linearGradient id="skill-bg" x1="0" x2="1" y1="0" y2="1">',
            '      <stop offset="0" stop-color="#ECFDF5"/>',
            '      <stop offset="1" stop-color="#F0FDFA"/>',
            "    </linearGradient>",
            '    <pattern id="grid" width="36" height="36" patternUnits="userSpaceOnUse">',
            '      <path d="M 36 0 L 0 0 0 36" fill="none" stroke="#CBD5E1" stroke-width="1" opacity="0.28"/>',
            "    </pattern>",
            '    <filter id="shadow" x="-12%" y="-12%" width="124%" height="130%">',
            '      <feDropShadow dx="0" dy="8" stdDeviation="10" flood-color="#0F172A" flood-opacity="0.15"/>',
            "    </filter>",
            '    <filter id="tight-shadow" x="-8%" y="-8%" width="116%" height="120%">',
            '      <feDropShadow dx="0" dy="4" stdDeviation="5" flood-color="#0F172A" flood-opacity="0.14"/>',
            "    </filter>",
            '    <filter id="endpoint-glow" x="-18%" y="-18%" width="136%" height="136%">',
            '      <feDropShadow dx="0" dy="0" stdDeviation="9" flood-color="#38BDF8" flood-opacity="0.38"/>',
            "    </filter>",
            arrow_marker("arrow-slate", "#64748B"),
            arrow_marker("arrow-blue", "#2563EB"),
            arrow_marker("arrow-indigo", "#4F46E5"),
            arrow_marker("arrow-amber", "#D97706"),
            arrow_marker("arrow-green", "#16A34A"),
            arrow_marker("arrow-rose", "#E11D48"),
            "  </defs>",
        ]
    )


def canvas(title: str, subtitle: str, *, accent: str = "#16A34A") -> list[str]:
    return [
        rect(0, 0, W, H, fill="#F8FAFC", rx=0),
        rect(0, 0, W, 92, fill="#0F172A", rx=0),
        text(W / 2, 30, title, size=25, fill="#FFFFFF", weight=800, anchor="middle"),
        text(W / 2, 62, subtitle, size=13, fill="#CBD5E1", weight=500, anchor="middle"),
        rect(W / 2 - 104, 87, 208, 4, fill=accent, rx=2),
    ]


def save_svg(filename: str, body: list[str], *, title: str, desc: str, width: int = W, height: int = H) -> None:
    path_out = Path(filename)
    svg = "\n".join(
        [
            '<?xml version="1.0" encoding="UTF-8"?>',
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}" font-family="{FONT}">',
            f"  <title>{escape(title)}</title>",
            f"  <desc>{escape(desc)}</desc>",
            base_defs(),
            *body,
            "</svg>",
            "",
        ]
    )
    path_out.write_text(svg, encoding="utf-8")
    print(f"Saved {len(body)} elements -> {path_out} [{width}x{height}]")
