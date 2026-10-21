#!/usr/bin/env python3
"""Manifest assembly + safe-by-default redaction guardrails.

STDLIB + pyyaml only. Turns the raw per-relation extraction into the reviewable
``estate-manifest-<ts>.yaml`` document Data Workbench imports. The redaction pass
is the privacy backbone: a PII-classified column, an over-cardinality column, or
a too-long free-text column keeps its *counts* but has its value-bearing outputs
(``min`` / ``max`` / ``top_values``) NULLED — and the withholding is recorded in
the manifest's ``redaction`` summary so the reviewer sees exactly what was held
back. ``--no-values`` drops every value-bearing field globally.
"""
from __future__ import annotations

import os
from typing import Any

import yaml

from _extract_core import is_pii

MANIFEST_VERSION = "1"
TOOL_VERSION = "1"


class RedactionConfig:
    def __init__(self, *, include_values: bool, max_enum_cardinality: int,
                 max_value_length: int, pii_tokens: tuple):
        self.include_values = include_values
        self.max_enum_cardinality = max_enum_cardinality
        self.max_value_length = max_value_length
        self.pii_tokens = pii_tokens


def guard_profile(profile: dict[str, Any], col_name: str,
                  cfg: RedactionConfig) -> tuple[dict[str, Any], str]:
    """Apply the redaction guardrails to one profile in place. Returns
    ``(clean_profile, reason)`` where ``reason`` ∈ ``{"", "pii",
    "length_capped", "cardinality_capped", "values_off"}``."""
    distinct = int(profile.pop("_distinct", 0) or 0)
    max_len = profile.get("max_length")
    profile.pop("_kind", None)
    profile.pop("_sampled", None)

    pii = is_pii(col_name, cfg.pii_tokens)
    over_len = max_len is not None and max_len > cfg.max_value_length
    over_card = distinct > cfg.max_enum_cardinality

    reason = ""
    if not cfg.include_values:
        reason = "values_off"
    elif pii:
        reason = "pii"
    elif over_len:
        reason = "length_capped"
    elif over_card:
        reason = "cardinality_capped"

    if reason:
        # keep counts + aggregate stats (null_count/distinct/mean/lengths); null the
        # raw value-bearing fields (a min/max/top_value exposes actual data).
        profile["min"] = None
        profile["max"] = None
        profile["top_values"] = None
        profile["redacted"] = True
        profile["redaction_reason"] = reason
    else:
        profile["redacted"] = False
    # drop null value-bearing keys to keep the YAML clean + reviewable
    return {k: v for k, v in profile.items() if v is not None or k in ("min", "max")}, reason


def build_manifest(
    *,
    platform: str,
    catalog: str,
    relations: list[dict[str, Any]],
    code_assets: list[dict[str, Any]],
    cfg: RedactionConfig,
    include_metadata: bool = True,
    include_volumetrics: bool = True,
    include_profiling: bool = True,
    include_code_assets: bool = False,
    generated_at: str = "",
) -> dict[str, Any]:
    """Assemble the manifest dict. ``relations`` carry an in-progress
    ``columns[].profile`` (raw); this applies the guardrails + tallies the
    redaction summary."""
    red = {"pii_redacted": 0, "cardinality_capped": 0, "length_capped": 0, "values_off": 0}
    _reason_key = {"pii": "pii_redacted", "cardinality_capped": "cardinality_capped",
                   "length_capped": "length_capped", "values_off": "values_off"}
    out_relations: list[dict[str, Any]] = []
    for rel in relations:
        cols_out = []
        for c in rel.get("columns", []) or []:
            col: dict[str, Any] = {
                "name": c["name"],
                "data_type": c.get("data_type", ""),
                "nullable": bool(c.get("nullable", True)),
                "ordinal": c.get("ordinal"),
            }
            if c.get("comment"):
                col["comment"] = c["comment"]
            if c.get("primary_key"):
                col["primary_key"] = True
            prof = c.get("profile")
            if include_profiling and prof is not None:
                clean, reason = guard_profile(dict(prof), c["name"], cfg)
                key = _reason_key.get(reason)
                if key:
                    red[key] += 1
                col["profile"] = clean
            cols_out.append(col)
        rel_out: dict[str, Any] = {
            "schema": rel["schema"],
            "table": rel["table"],
            "relation_kind": rel.get("relation_kind", "table"),
        }
        if include_volumetrics:
            for k in ("row_count", "row_count_is_estimate", "size_bytes",
                      "last_modified", "num_files"):
                if rel.get(k) is not None:
                    rel_out[k] = rel[k]
        if rel.get("comment"):
            rel_out["comment"] = rel["comment"]
        rel_out["columns"] = cols_out
        if rel.get("foreign_keys"):
            rel_out["foreign_keys"] = rel["foreign_keys"]
        out_relations.append(rel_out)

    return {
        "manifest_version": MANIFEST_VERSION,
        "kind": "estate",
        "platform": platform,
        "catalog": catalog or None,
        "generated_at": generated_at,
        "tool_version": TOOL_VERSION,
        "extraction": {
            "metadata": include_metadata,
            "volumetrics": include_volumetrics,
            "profiling": include_profiling,
            "values_included": cfg.include_values,
            "code_assets": include_code_assets,
            "redaction": {k: v for k, v in red.items() if v},
        },
        "relations": out_relations,
        "code_assets": code_assets or [],
    }


def write_yaml_atomic(path: str, payload: dict[str, Any]) -> None:
    """Atomic write (temp + replace) so a partial run never leaves a truncated
    manifest — mirrors ``estate_ingest.write_estate`` / the export skills."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        yaml.safe_dump(payload, fh, sort_keys=False, allow_unicode=True, default_flow_style=False)
    os.replace(tmp, path)
