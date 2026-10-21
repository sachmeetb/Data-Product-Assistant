"""Registry-driven platform validation for intake blueprints.

The parser emits *human* platform strings ("Oracle", "Databricks", "SQL
Server") with high confidence that the *report said* so — but that says nothing
about whether Data Workbench can actually execute a migration against them. This
module bridges the two: it resolves a free-form platform string to a manifest
id (``platform/registry.py``) and checks the real capability level, so the
intake reviewer sees "not runnable" *before* scaffolding a dead-on-arrival
project.

Advisories are **non-blocking** by design — a practitioner may legitimately want
to stage a placeholder project for a platform DWB doesn't support yet. They are
surfaced loudly in the review UI, not folded into the approval gate.
"""
from __future__ import annotations

from typing import Any, Optional

from .platform.registry import get_registry

# Free-form parser strings → manifest ids. Lowercased before lookup. The
# registry ids are the canonical set (``registry.platform_ids()``); this only
# maps common vendor spellings the parser is likely to emit.
_ALIASES: dict[str, str] = {
    "postgres": "postgres",
    "postgresql": "postgres",
    "postgres db": "postgres",
    "pg": "postgres",
    "mysql": "mysql",
    "mariadb": "mysql",
    "oracle": "oracle",
    "oracle db": "oracle",
    "oracle database": "oracle",
    "oracle 19c": "oracle",
    "sqlserver": "sqlserver",
    "sql server": "sqlserver",
    "mssql": "sqlserver",
    "microsoft sql server": "sqlserver",
    "azure sql": "sqlserver",
    "azure sql database": "sqlserver",
    "databricks": "databricks",
    "databricks lakehouse": "databricks",
    "unity catalog": "databricks",
    "snowflake": "snowflake",
    "duckdb": "duckdb_local",
    "duckdb_local": "duckdb_local",
    "s3": "s3",
    "amazon s3": "s3",
    "aws s3": "s3",
    "gcs": "gcs",
    "google cloud storage": "gcs",
    "adls": "azure_adls",
    "azure_adls": "azure_adls",
    "azure data lake storage": "azure_adls",
}


def resolve_platform(raw: Optional[str]) -> Optional[str]:
    """Map a free-form platform string to a registry manifest id, or None.

    Tries the alias table first, then a direct id match, then a display-name
    substring match so slightly-off spellings still resolve.
    """
    if not raw:
        return None
    key = raw.strip().lower()
    if not key:
        return None
    if key in _ALIASES:
        return _ALIASES[key]
    reg = get_registry()
    if reg.get_manifest(key) is not None:
        return key
    # last-ditch: match a known display name (e.g. "postgresql" text in body)
    for pid in reg.platform_ids():
        m = reg.get_manifest(pid)
        if m and pid in key:
            return pid
    return None


def _advisory(
    field: str, raw: Optional[str], resolved: Optional[str], capability: str, label: str
) -> Optional[dict[str, Any]]:
    """Build one advisory for a platform field, or None when it's fine."""
    if not raw:
        return None  # missing values are handled by the gap machinery, not here
    reg = get_registry()
    usable = sorted(pid for pid in reg.platform_ids() if reg.is_usable(pid, capability))
    if resolved is None:
        return {
            "field": field,
            "platform": raw,
            "resolved_id": None,
            "level": "unknown",
            "severity": "error",
            "message": (
                f"{label} “{raw}” isn’t a platform Data Workbench recognizes for "
                f"{capability}. Supported: {', '.join(usable)}."
            ),
        }
    if not reg.is_usable(resolved, capability):
        level = reg.get_capability(resolved, capability).value
        return {
            "field": field,
            "platform": raw,
            "resolved_id": resolved,
            "level": level,
            "severity": "error",
            "message": (
                f"{label} “{raw}” ({resolved}) isn’t supported for "
                f"{capability} (level: {level}). This project can be scaffolded "
                f"but won’t be runnable. Supported: {', '.join(usable)}."
            ),
        }
    # usable — a quiet confirmation the UI can render green
    level = reg.get_capability(resolved, capability).value
    return {
        "field": field,
        "platform": raw,
        "resolved_id": resolved,
        "level": level,
        "severity": "ok",
        "message": f"{label} “{raw}” → {resolved} ({level}).",
    }


def platform_advisories(blueprint: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return per-field platform advisories for a blueprint (migration only).

    Modernization blueprints don't carry a single source/target platform, so
    they yield no advisories here.
    """
    if not blueprint or blueprint.get("scenario") != "migration":
        return []
    out: list[dict[str, Any]] = []
    src_raw = (blueprint.get("source_platform") or {}).get("value")
    tgt_raw = (blueprint.get("target_platform") or {}).get("value")
    a = _advisory("source_platform", src_raw, resolve_platform(src_raw), "transfer_source", "Source platform")
    if a:
        out.append(a)
    a = _advisory("target_platform", tgt_raw, resolve_platform(tgt_raw), "transfer_target", "Target platform")
    if a:
        out.append(a)
    return out
