"""Read-only platform registry API.

Exposes the capability matrix and per-platform manifests so the
connection-management UI and the MCP layer can list available
platforms and their supported capabilities without touching the
registry singleton directly.

All endpoints are GET-only; the registry is loaded at import time and
is immutable after startup.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..platform.registry import get_registry
from ..platform.interfaces import CapabilityLevel

router = APIRouter(prefix="/api/platforms", tags=["platforms"])


@router.get("")
def list_platforms():
    """Return all registered platforms with their display names and
    top-level usability flags."""
    reg = get_registry()
    result = []
    for pid in reg.platform_ids():
        m = reg.get_manifest(pid)
        result.append({
            "platform_id": pid,
            "display_name": m.display_name,
            "adapter_version": m.adapter_version,
            "adapter_kind": m.adapter_kind,
            "supported_server_versions": m.supported_server_versions,
            # Per-capability usability booleans for quick feature-flag checks.
            "usable": {
                cap: reg.is_usable(pid, cap)
                for cap in m.capabilities
            },
        })
    return {"platforms": result, "count": len(result)}


@router.get("/capability-matrix")
def capability_matrix():
    """Return the full capability-level matrix across all platforms.

    Values are the raw `CapabilityLevel` strings (unsupported / experimental /
    preview / certified / deprecated) so the UI can render colour-coded cells.
    """
    return {"matrix": get_registry().capability_matrix()}


@router.get("/{platform_id}")
def get_platform(platform_id: str):
    """Return the manifest for a single platform."""
    reg = get_registry()
    m = reg.get_manifest(platform_id)
    if m is None:
        raise HTTPException(
            status_code=404,
            detail=f"Platform '{platform_id}' is not registered. "
                   f"Known platforms: {reg.platform_ids()}",
        )
    return {
        "platform_id": m.id,
        "display_name": m.display_name,
        "adapter_version": m.adapter_version,
        "adapter_kind": m.adapter_kind,
        "supported_server_versions": m.supported_server_versions,
        "namespace": m.namespace,
        "capabilities": {cap: lvl.value for cap, lvl in m.capabilities.items()},
        "notes": m.notes,
    }


@router.get("/{platform_id}/capabilities/{capability}")
def get_capability(platform_id: str, capability: str):
    """Return the level for one platform-capability pair.

    Always returns a result (never 404) — unknown platforms or capabilities
    return `unsupported` to match the registry's fail-closed contract.
    """
    reg = get_registry()
    level = reg.get_capability(platform_id, capability)
    return {
        "platform_id": platform_id,
        "capability": capability,
        "level": level.value,
        "usable": reg.is_usable(platform_id, capability),
    }
