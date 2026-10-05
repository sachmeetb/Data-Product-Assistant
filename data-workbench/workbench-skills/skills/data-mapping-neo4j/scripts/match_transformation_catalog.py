#!/usr/bin/env python3
"""
Match product columns against a steward-curated transformation catalog.

The catalog lives at ``playbook/transformation_catalogs/<domain>.yaml`` (a
sibling pattern to ``playbook/domain_catalogs/<domain>.yaml``). Each template
declares a set of match rules and a transform recipe. This script takes a
list of product columns and a list of available source columns and returns
the templates that fit.

Catalog YAML shape:

    domain: common
    templates:
      - name: full_name_concat
        match:
          target_name_patterns:    ["full_name", "fullname", "complete_name"]
          target_logical_type:     "string"
          source_columns_required: ["first_name", "last_name"]
          source_data_type_pattern: "VARCHAR\\\\([0-9]+\\\\)"
        transform:
          kind: concat
          inputs: ["first_name", "last_name"]
          separator: " "
          decorators:
            standardization: ["trim"]
        confidence_floor: 0.85

Match semantics:
  - target_name_patterns:    case-insensitive substring match against product column name
  - target_logical_type:     case-insensitive equality against product column logicalType
  - source_columns_required: every name must be present in the source-column set (by name)
  - source_data_type_pattern: regex matched against the FIRST required source column's
                               dataType; non-matching templates skipped.

A template can omit `match` entirely; in that case it never matches automatically
(it is only a library entry the agent could invoke explicitly).

Usage:
  python match_transformation_catalog.py \\
      --product-columns product_cols.json \\
      --source-columns source_cols.json \\
      --domain hr \\
      [--catalog-dir /path/to/playbook/transformation_catalogs] \\
      [--output matches.json]

product_cols.json: [{"column_uri": "...", "column_name": "...",
                     "data_type": "...", "logical_type": "...",
                     "description": "..."}]

source_cols.json:  [{"column_uri": "...", "column_name": "...",
                     "data_type": "..."}]

Output: [{"product_column_uri": "...", "template_name": "...",
          "domain": "...", "transform": {...}, "confidence": 0.85}]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any, Optional

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml is required. Install with: pip install pyyaml", file=sys.stderr)
    sys.exit(1)


def _load_catalog_files(catalog_dir: str, domain: Optional[str]) -> list[dict]:
    """Load catalog YAML files from the directory.

    If `domain` is given, loads `<catalog_dir>/<domain>.yaml` first then
    `<catalog_dir>/common.yaml` if it exists. Otherwise loads every `*.yaml`.
    Returns a list of {domain, templates[]} dicts. Missing dir → [].
    """
    if not catalog_dir or not os.path.isdir(catalog_dir):
        return []

    files: list[str] = []
    if domain:
        domain_path = os.path.join(catalog_dir, f"{domain}.yaml")
        if os.path.isfile(domain_path):
            files.append(domain_path)
        common_path = os.path.join(catalog_dir, "common.yaml")
        if os.path.isfile(common_path) and common_path not in files:
            files.append(common_path)
    else:
        for name in sorted(os.listdir(catalog_dir)):
            if name.endswith(".yaml") or name.endswith(".yml"):
                files.append(os.path.join(catalog_dir, name))

    out: list[dict] = []
    for fp in files:
        try:
            with open(fp) as f:
                doc = yaml.safe_load(f) or {}
        except Exception as exc:
            print(f"WARNING: failed to read {fp}: {exc}", file=sys.stderr)
            continue
        if not isinstance(doc, dict):
            continue
        templates = doc.get("templates") or []
        if not isinstance(templates, list):
            continue
        out.append({"domain": doc.get("domain") or os.path.splitext(os.path.basename(fp))[0],
                    "templates": templates,
                    "_source": fp})
    return out


def _matches_target_name(name: str, patterns: list[str]) -> bool:
    if not patterns:
        return True
    name_l = (name or "").lower()
    return any(p and p.lower() in name_l for p in patterns)


def _matches_logical_type(actual: str, expected: Optional[str]) -> bool:
    if not expected:
        return True
    return (actual or "").strip().lower() == expected.strip().lower()


def _has_required_sources(required: list[str], source_names: set[str]) -> bool:
    if not required:
        return True
    return all((n or "").lower() in source_names for n in required)


def _matches_source_dtype(pattern: Optional[str], required: list[str], source_lookup: dict[str, str]) -> bool:
    if not pattern:
        return True
    if not required:
        return True
    first = source_lookup.get((required[0] or "").lower())
    if first is None:
        return False
    try:
        return re.search(pattern, first) is not None
    except re.error:
        return False


def match_templates(
    product_columns: list[dict],
    source_columns: list[dict],
    catalog_dir: str,
    domain: Optional[str] = None,
) -> list[dict]:
    """
    For each product column, find templates whose match rules are satisfied
    against the available source columns. Returns the cross-product of
    (product_column, matching_template) — multiple templates may match a column.
    """
    catalogs = _load_catalog_files(catalog_dir, domain)
    if not catalogs:
        return []

    source_names = {(c.get("column_name") or "").lower() for c in source_columns}
    source_lookup = {(c.get("column_name") or "").lower(): c.get("data_type") or ""
                     for c in source_columns}

    matches: list[dict] = []
    for pc in product_columns:
        pc_name = pc.get("column_name") or ""
        pc_logical = pc.get("logical_type") or pc.get("logicalType") or ""

        for cat in catalogs:
            for tmpl in cat["templates"]:
                if not isinstance(tmpl, dict):
                    continue
                m = tmpl.get("match") or {}
                if not isinstance(m, dict):
                    continue

                if not _matches_target_name(pc_name, m.get("target_name_patterns") or []):
                    continue
                if not _matches_logical_type(pc_logical, m.get("target_logical_type")):
                    continue
                req = m.get("source_columns_required") or []
                if not _has_required_sources(req, source_names):
                    continue
                if not _matches_source_dtype(m.get("source_data_type_pattern"), req, source_lookup):
                    continue

                matches.append({
                    "product_column_uri": pc.get("column_uri"),
                    "product_column_name": pc_name,
                    "template_name": tmpl.get("name") or "(unnamed)",
                    "domain": cat["domain"],
                    "transform": tmpl.get("transform") or {},
                    "confidence": float(tmpl.get("confidence_floor") or 0.7),
                    "_source_file": cat["_source"],
                })

    return matches


def main():
    parser = argparse.ArgumentParser(
        description="Match product columns against a transformation catalog.",
    )
    parser.add_argument("--product-columns", required=True,
                        help="JSON file with [{column_uri, column_name, data_type, logical_type, ...}]")
    parser.add_argument("--source-columns", required=True,
                        help="JSON file with [{column_uri, column_name, data_type, ...}]")
    parser.add_argument("--catalog-dir",
                        default="playbook/transformation_catalogs",
                        help="Directory containing <domain>.yaml catalog files (default: playbook/transformation_catalogs).")
    parser.add_argument("--domain", default=None,
                        help="Restrict to <domain>.yaml (plus common.yaml). Omit to load all yaml files.")
    parser.add_argument("--output", default=None, help="Write matches to this JSON file (default: stdout).")
    args = parser.parse_args()

    with open(args.product_columns) as f:
        product_cols = json.load(f)
    with open(args.source_columns) as f:
        source_cols = json.load(f)

    matches = match_templates(product_cols, source_cols, args.catalog_dir, args.domain)

    payload = json.dumps(matches, indent=2)
    if args.output:
        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
        with open(args.output, "w") as f:
            f.write(payload + "\n")
        print(f"Wrote {len(matches)} match(es) to {args.output}")
    else:
        print(payload)


if __name__ == "__main__":
    main()
