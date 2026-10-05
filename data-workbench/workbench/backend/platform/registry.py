"""Platform registry — single choke point for all platform dispatching.

Loads capability manifests from the manifests/ directory at startup.
Resolves the effective capability level for a platform-capability pair
as the intersection of:
  1. The adapter manifest's declared maximum
  2. The live instance probe (if available)
  3. Environment/tenant policy overrides (future)

No code outside this module should import a vendor driver or branch
on a platform name string.  Unknown or unsupported platforms always
return capability_unavailable, never a silent Postgres fallback.
"""
from __future__ import annotations

import logging
import pathlib
from dataclasses import dataclass, field
from typing import Any, Optional

import yaml

from .interfaces import CapabilityLevel

logger = logging.getLogger(__name__)

_MANIFEST_DIR = pathlib.Path(__file__).parent / "manifests"


@dataclass
class PlatformManifest:
    id: str
    display_name: str
    adapter_version: str
    adapter_kind: str                            # "jdbc" | "embedded"
    supported_server_versions: str
    driver: dict[str, str]
    namespace: dict[str, Any]
    capabilities: dict[str, CapabilityLevel]
    notes: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


def _load_manifest(path: pathlib.Path) -> PlatformManifest:
    with open(path) as f:
        raw = yaml.safe_load(f)
    caps = {
        k: CapabilityLevel(v)
        for k, v in (raw.get("capabilities") or {}).items()
    }
    return PlatformManifest(
        id=raw["id"],
        display_name=raw["displayName"],
        adapter_version=raw["adapterVersion"],
        adapter_kind=raw.get("adapterKind", "jdbc"),
        supported_server_versions=raw.get("supportedServerVersions", "*"),
        driver=raw.get("driver", {}),
        namespace=raw.get("namespace", {}),
        capabilities=caps,
        notes=raw.get("notes", ""),
        raw=raw,
    )


class PlatformRegistry:
    """Loaded once at startup; immutable after that."""

    def __init__(self) -> None:
        self._manifests: dict[str, PlatformManifest] = {}
        self._load_all()

    def _load_all(self) -> None:
        if not _MANIFEST_DIR.is_dir():
            logger.warning("platform manifests directory not found: %s", _MANIFEST_DIR)
            return
        for path in sorted(_MANIFEST_DIR.glob("*.yaml")):
            try:
                m = _load_manifest(path)
                self._manifests[m.id] = m
                logger.debug("loaded platform manifest: %s v%s", m.id, m.adapter_version)
            except Exception:
                logger.exception("failed to load platform manifest: %s", path)

    # ── public API ────────────────────────────────────────────────────────────

    def platform_ids(self) -> list[str]:
        return sorted(self._manifests.keys())

    def get_manifest(self, platform_id: str) -> Optional[PlatformManifest]:
        return self._manifests.get(platform_id)

    def get_capability(
        self, platform_id: str, capability: str
    ) -> CapabilityLevel:
        """Return the declared capability level.

        Unknown platform or capability always returns UNSUPPORTED —
        never falls back silently to Postgres (ADR-9).
        """
        m = self._manifests.get(platform_id)
        if m is None:
            return CapabilityLevel.UNSUPPORTED
        return m.capabilities.get(capability, CapabilityLevel.UNSUPPORTED)

    def is_usable(self, platform_id: str, capability: str) -> bool:
        """Return True only when the capability is PREVIEW or CERTIFIED."""
        level = self.get_capability(platform_id, capability)
        return level in (CapabilityLevel.PREVIEW, CapabilityLevel.CERTIFIED)

    def capability_matrix(self) -> dict[str, dict[str, str]]:
        """Return a flat matrix {platform_id: {capability: level}} for UI/API."""
        return {
            mid: {cap: lvl.value for cap, lvl in m.capabilities.items()}
            for mid, m in self._manifests.items()
        }

    def assert_usable(self, platform_id: str, capability: str) -> None:
        """Raise CapabilityUnavailable if the capability is not usable.

        Call this at the entry point of any operation that requires a
        specific platform capability.  The message is actionable — it
        tells the caller what the current level is and what to check.
        """
        level = self.get_capability(platform_id, capability)
        if level not in (CapabilityLevel.PREVIEW, CapabilityLevel.CERTIFIED):
            raise CapabilityUnavailable(
                f"Platform '{platform_id}' capability '{capability}' is "
                f"'{level.value}' — not available for use. "
                f"Check the platform manifest or contact the adapter owner."
            )


class CapabilityUnavailable(Exception):
    """Raised by the registry when a requested capability is not usable.

    Callers should convert this to HTTP 422 or MCP capability_unavailable.
    Never catch and silently fall back to another platform.
    """


# ── module-level singleton ────────────────────────────────────────────────────

_registry: Optional[PlatformRegistry] = None


def get_registry() -> PlatformRegistry:
    """Return the module-level singleton registry, loading on first call."""
    global _registry
    if _registry is None:
        _registry = PlatformRegistry()
    return _registry
