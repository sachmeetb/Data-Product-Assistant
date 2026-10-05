"""Helpers for the stable-node + temporal-edge contract model.

Replaces the legacy "clone substructure per version" pattern. Every
:DataContract is now a single stable node keyed on `id`; versions are
tracked via :ContractVersion sidecars (one per version) attached by
`:HAS_VERSION`. Substructure (:DataContractProperty, :DataContractSchema,
:DataContractQuality, owner-types, etc.) consists of stable nodes keyed on
logical identity URIs. Edges from :DataContract to its substructure carry
temporal validity ranges (`fromVersion` / `toVersion`).

This module exposes:

- Constants describing the schema (relationship names, label names).
- WHERE-clause builders for current-version and version-pinned reads.
- URI builders for the stable substructure node identities.

Every callsite that needs to walk substructure should compose its Cypher
through these helpers so the validity-range filter is written exactly once.
"""

from __future__ import annotations


# ── Stable URI builders ───────────────────────────────────────────────────

def schema_uri(contract_id: str, physical_name: str) -> str:
    return f"contract:{contract_id}:schema:{physical_name}"


def property_uri(contract_id: str, schema_physical_name: str, property_physical_name: str) -> str:
    return (
        f"contract:{contract_id}:property:"
        f"{schema_physical_name}:{property_physical_name}"
    )


def owner_uri(contract_id: str, key: str) -> str:
    """`key` is the owner's email (preferred) or username; whichever is non-empty.

    Owners without either identity fall back to a positional key the caller
    must supply — typically the index into the spec's owners[] array.
    """
    return f"contract:{contract_id}:owner:{key}"


def steward_uri(contract_id: str, key: str) -> str:
    return f"contract:{contract_id}:steward:{key}"


def team_member_uri(contract_id: str, key: str) -> str:
    return f"contract:{contract_id}:team:{key}"


def role_uri(contract_id: str, key: str) -> str:
    return f"contract:{contract_id}:role:{key}"


def server_uri(contract_id: str, name: str) -> str:
    return f"contract:{contract_id}:server:{name}"


def quality_uri(contract_id: str, key: str) -> str:
    """Quality rules have no natural unique key in ODCS — caller hashes the
    rule shape (rule + column + dataset + dimension) or assigns a UUID.

    `key` is whatever stable identifier the caller computed.
    """
    return f"contract:{contract_id}:quality:{key}"


def sla_property_uri(contract_id: str, name: str) -> str:
    return f"contract:{contract_id}:sla:{name}"


def terms_uri(contract_id: str) -> str:
    """Terms are a singleton per contract — one block, not a list."""
    return f"contract:{contract_id}:terms"


def dataset_transform_uri(contract_id: str, schema_physical_name: str) -> str:
    return f"contract:{contract_id}:transform:{schema_physical_name}"


def contract_version_uri(contract_id: str, version: int) -> str:
    return f"contract:{contract_id}:version:{version}"


# ── Cypher fragments ──────────────────────────────────────────────────────

# Standard relationship-variable convention: `r` for the contract→substructure
# edge. Compose into queries:
#
#     MATCH (dc:DataContract {id: $cid})-[r:HAS_PROPERTY]->(p)
#     WHERE """ + current_version_filter() + """
#     RETURN p
#
# For nested traversals (e.g. dc → schema → property), use distinct rel vars
# (`r1` / `r2`) and call this twice.

def current_version_filter(rel_var: str = "r", contract_var: str = "dc") -> str:
    """Filter a temporal edge to only those rows valid at the contract's currentVersion."""
    return (
        f"{rel_var}.fromVersion <= {contract_var}.currentVersion "
        f"AND ({rel_var}.toVersion IS NULL OR {rel_var}.toVersion >= {contract_var}.currentVersion)"
    )


def version_pin_filter(rel_var: str = "r", version_param: str = "$version") -> str:
    """Filter a temporal edge to only those rows valid at a specific version."""
    return (
        f"{rel_var}.fromVersion <= {version_param} "
        f"AND ({rel_var}.toVersion IS NULL OR {rel_var}.toVersion >= {version_param})"
    )


# ── Lifecycle-state semantics ─────────────────────────────────────────────

# A contract whose currentLifecycleState is in this set can be edited in place
# without branching a new version (today's "MERGE in place" path). Any other
# state requires either a cosmetic-patch path or a MERGE_NEW_VERSION branch.
DRAFT_STATES = {None, "draft", "ingesting"}

# Marketplace shows products in these states. `superseded` means the version
# has been replaced by a newer one but is still browsable historically.
PUBLIC_STATES = {"published", "superseded"}
