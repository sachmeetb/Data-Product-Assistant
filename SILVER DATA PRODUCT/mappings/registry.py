"""
registry.py — Declarative Cross-Domain & Cross-Industry Mapping Specification Registry.

Loads YAML domain mapping specifications from `mappings/domains/<industry>/<domain>.yaml`
so the Silver Conformance & STTM Engine operates via reusable metadata rather than
hardcoded 4-to-1 Python functions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional
import yaml

_DOMAINS_DIR = Path(__file__).resolve().parent / "domains"

_ALIASES = {
    ("banking", "customer_360"): _DOMAINS_DIR / "banking" / "customer_360.yaml",
    ("banking", "customer"): _DOMAINS_DIR / "banking" / "customer_360.yaml",
    ("water", "account_360"): _DOMAINS_DIR / "water" / "account_360.yaml",
    ("energy_industrial", "account_360"): _DOMAINS_DIR / "water" / "account_360.yaml",
    ("energy_industrial", "water_treatment"): _DOMAINS_DIR / "water" / "account_360.yaml",
    ("water", "customer_360"): _DOMAINS_DIR / "water" / "account_360.yaml",
}


def load_domain_mapping(industry: str = "banking", domain: str = "customer_360") -> Dict[str, Any]:
    """Load a declarative N-to-M domain mapping YAML specification."""
    key = (industry.strip().lower(), domain.strip().lower())
    yaml_path = _ALIASES.get(key)
    if not yaml_path or not yaml_path.exists():
        candidate = _DOMAINS_DIR / key[0] / f"{key[1]}.yaml"
        if candidate.exists():
            yaml_path = candidate
        else:
            yaml_path = _DOMAINS_DIR / "banking" / "customer_360.yaml"

    with open(yaml_path, "r", encoding="utf-8") as f:
        spec = yaml.safe_load(f) or {}
    spec["_spec_path"] = str(yaml_path)
    return spec


def list_available_mappings() -> List[Dict[str, Any]]:
    """List all registered reusable cross-industry domain mapping specifications."""
    results: List[Dict[str, Any]] = []
    if not _DOMAINS_DIR.exists():
        return results
    for ypath in sorted(_DOMAINS_DIR.rglob("*.yaml")):
        with open(ypath, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        src_systems = data.get("source_systems") or []
        total_src_tables = sum(len(s.get("tables") or []) for s in src_systems)
        tgt_tables = data.get("target_silver_tables") or []
        results.append({
            "industry": data.get("industry"),
            "industry_name": data.get("industry_name"),
            "domain": data.get("domain"),
            "domain_display_name": data.get("domain_display_name"),
            "mapping_version": data.get("mapping_version", "3.0.0"),
            "architecture_model": data.get("architecture_model"),
            "source_system_count": len(src_systems),
            "source_table_count": total_src_tables,
            "target_silver_table_count": len(tgt_tables),
            "source_systems": [
                {
                    "source_system": s.get("source_system"),
                    "system_name": s.get("system_name"),
                    "ingestion_modes": s.get("ingestion_modes", []),
                    "tables": [t.get("table_name") for t in (s.get("tables") or [])],
                }
                for s in src_systems
            ],
            "target_silver_tables": [t.get("table_name") for t in tgt_tables],
        })
    return results
