"""
knowledge_tool.py — Load and query banking knowledge base files.

Provides functions to load the banking ontology, standards, BIAN service
domains, naming conventions, and validation rules from the knowledge/ dir.
All files are cached in-memory after first load.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_KNOWLEDGE_DIR = Path(__file__).parent.parent / "knowledge"
_cache: dict = {}


def _load_json(filename: str) -> dict:
    if filename in _cache:
        return _cache[filename]
    path = _KNOWLEDGE_DIR / filename
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        _cache[filename] = data
        return data
    except Exception as exc:
        log.warning("Could not load %s: %s", path, exc)
        return {}


def _load_yaml(filename: str) -> dict:
    if filename in _cache:
        return _cache[filename]
    path = _KNOWLEDGE_DIR / filename
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        _cache[filename] = data
        return data
    except Exception as exc:
        log.warning("Could not load %s: %s", path, exc)
        return {}


# ── Public accessors ──────────────────────────────────────────────────────────

def get_banking_ontology() -> dict:
    return _load_json("banking_ontology.json")


def get_banking_standards() -> dict:
    return _load_json("banking_standards.json")


def get_bian_domains() -> list[dict]:
    return _load_json("bian_service_domains.json").get("service_domains", [])


def get_naming_conventions() -> dict:
    return _load_yaml("naming_conventions.yaml")


def get_validation_rules() -> dict:
    return _load_yaml("validation_rules.yaml")


# ── Query helpers ─────────────────────────────────────────────────────────────

def get_blocks_for_banking_domain(domain_name: str) -> list[str]:
    """
    Return the recommended common blocks for a given banking domain.
    e.g. get_blocks_for_banking_domain("payments") → ["technical-metadata", "money", "identifier", ...]

    NOTE: This replaces the old get_entities_for_domain() which returned entity names.
    The new architecture builds tables from common blocks, not pre-built entity schemas.
    """
    from tools.schema_loader import get_domain_block_map
    bmap = get_domain_block_map()
    return bmap.get(domain_name, bmap.get("_all", ["technical-metadata"]))


# Backwards-compatible alias — deprecated, use get_blocks_for_banking_domain
def get_entities_for_domain(domain_name: str) -> list[str]:
    """
    Deprecated: returns entity names from the old ontology entity_to_domain_map.
    Retained for compatibility. Prefer get_blocks_for_banking_domain() in new code.
    """
    mapping = get_banking_ontology().get("entity_to_domain_map", {})
    return [entity for entity, domains in mapping.items() if domain_name in domains]


def get_bian_domains_for_keywords(keywords: list[str]) -> list[dict]:
    """Return BIAN service domains matching the given keywords."""
    kw_lower = [k.lower() for k in keywords]
    return [
        d for d in get_bian_domains()
        if any(
            kw in d.get("name", "").lower() or kw in d.get("domain", "").lower()
            for kw in kw_lower
        )
    ]


def get_regulatory_requirements(frameworks: list[str]) -> dict:
    """
    Return regulatory attribute requirements for given framework names.
    e.g. get_regulatory_requirements(["GDPR", "AML/KYC"])
    """
    reg_map = get_banking_ontology().get("regulatory_entity_requirements", {})
    result = {}
    for fw in frameworks:
        fw_key = fw.lower().replace("/", "_").replace("-", "_").replace(" ", "_")
        # Exact match
        if fw_key in reg_map:
            result[fw] = reg_map[fw_key]
            continue
        # Fuzzy match
        for key, val in reg_map.items():
            if fw_key in key or key in fw_key:
                result[fw] = val
                break
    return result


def get_bigquery_type(semantic_type: str) -> str:
    """Map a semantic type to the recommended BigQuery column type."""
    return get_banking_standards().get("bigquery_type_mappings", {}).get(
        semantic_type, "STRING"
    )


def get_technical_envelope_columns() -> list[dict]:
    """Return the list of required technical envelope column definitions."""
    return get_naming_conventions().get("technical_envelope", {}).get("required", [])


def knowledge_summary_for_agent() -> str:
    """
    Compact JSON string of key knowledge base metadata for agent prompts.
    Reflects the common-block architecture (not the old entity model).
    """
    from tools.schema_loader import get_common_block_catalog, get_domain_block_map

    ontology    = get_banking_ontology()
    standards   = get_banking_standards()
    bian        = get_bian_domains()
    conventions = get_naming_conventions()
    catalog     = get_common_block_catalog()

    summary = {
        "banking_sub_domains": list(
            ontology.get("domain_hierarchy", {})
                    .get("banking", {})
                    .get("sub_domains", {})
                    .keys()
        ),
        "key_standards":            list(standards.get("standards", {}).keys()),
        "bian_domain_count":        len(bian),
        "table_prefixes":           conventions.get("tables", {}).get("prefix", {}),
        "common_blocks_available":  list(catalog.keys()),
        "domain_block_map":         get_domain_block_map(),
        "bigquery_type_mappings":   standards.get("bigquery_type_mappings", {}),
    }
    return json.dumps(summary, indent=2)
