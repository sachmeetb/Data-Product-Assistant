"""Runtime reader for the transform capability artifact.

This module is the **sole** backend authority on which SQL functions / neutral
transform ops each served platform supports. It reads only the generated,
checksummed JSON artifact (``transform_capabilities.<schemaver>.json``) that the
``data-transform-translation`` skill's build step produces. **The backend never
imports the skill** — a client fork changes the artifact (a data change), not
this code.

Capability semantics:
  - ``native`` / ``emulated``  → SUPPORTED (the AST validator passes it).
  - ``unsupported``            → fail closed (error).
  - a function that is neither in ``portable_functions`` nor in any platform's
    map → **unknown** → fail closed (we never equate "sqlglot re-emitted it"
    with "the engine supports it").

Fail-closed on load: a missing artifact, a checksum mismatch, or a schema-shape
error raises ``CapabilityArtifactError`` rather than silently degrading.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_ARTIFACT_DIR = Path(__file__).resolve().parent
_ARTIFACT_GLOB = "transform_capabilities.v*.json"
_VERSION_RE = re.compile(r"transform_capabilities\.v(\d+)\.json$")

SUPPORTED = frozenset({"native", "emulated"})


class CapabilityArtifactError(RuntimeError):
    """Raised when the capability artifact is missing, corrupt, or fails its
    self-declared checksum. Fail-closed — never fall back to a guessed default."""


def _canonical_checksum(artifact: dict) -> str:
    """sha256 over the canonical JSON of the artifact body with the volatile
    ``checksum`` + ``generated_at`` fields removed.

    MUST stay byte-identical to
    ``data-transform-translation/scripts/build_artifact.py:canonical_checksum``
    (deliberately duplicated so the runtime does not import the skill).
    ``test_transform_capabilities_artifact.py`` guards the two against drift.
    """
    body = {k: v for k, v in artifact.items() if k not in ("checksum", "generated_at")}
    canon = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canon.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TransformCapabilities:
    """In-memory view of one capability artifact."""

    schema_version: str
    checksum: str
    platforms: tuple[str, ...]
    portable_functions: frozenset[str]
    functions: dict[str, dict]
    ops: dict[str, dict]
    source_path: Optional[Path] = None

    # ── functions ──────────────────────────────────────────────────────────
    def is_portable(self, name: str) -> bool:
        return (name or "").upper() in self.portable_functions

    def function_entry(self, name: str, platform: str) -> Optional[dict]:
        fn = self.functions.get((name or "").upper())
        if not fn:
            return None
        return fn.get("platforms", {}).get((platform or "").lower())

    def function_support(self, name: str, platform: str) -> Optional[str]:
        """Return the capability of ``name`` on ``platform``:
        'native' | 'emulated' | 'unsupported', or ``None`` when the function is
        UNKNOWN (not portable and absent from the platform's map). Callers treat
        both 'unsupported' and ``None`` as fail-closed.
        """
        key = (name or "").upper()
        if key in self.portable_functions:
            return "native"
        entry = self.function_entry(key, platform)
        if entry is None:
            return None
        return entry.get("capability")

    def is_function_supported(self, name: str, platform: str) -> bool:
        return self.function_support(name, platform) in SUPPORTED

    # ── ops ────────────────────────────────────────────────────────────────
    def op_entry(self, op: str, semantics: str, platform: str) -> Optional[dict]:
        node = self.ops.get(op)
        if not node:
            return None
        return node.get("semantics", {}).get(semantics, {}).get((platform or "").lower())

    def op_support(self, op: str, semantics: str, platform: str) -> Optional[str]:
        entry = self.op_entry(op, semantics, platform)
        if entry is None:
            return None
        return entry.get("capability")

    def op_semantics(self, op: str) -> list[str]:
        node = self.ops.get(op)
        return sorted(node.get("semantics", {})) if node else []

    # ── integrity ────────────────────────────────────────────────────────────
    def recomputed_checksum(self, raw: dict) -> str:
        return _canonical_checksum(raw)


def _artifact_files(directory: Path) -> list[tuple[int, Path]]:
    out: list[tuple[int, Path]] = []
    for p in directory.glob(_ARTIFACT_GLOB):
        m = _VERSION_RE.search(p.name)
        if m:
            out.append((int(m.group(1)), p))
    return sorted(out)


def load_capabilities(path: Optional[Path | str] = None) -> TransformCapabilities:
    """Load + verify a capability artifact.

    ``path`` given → load that file. Otherwise pick the highest ``vN`` artifact
    in this package directory. Raises ``CapabilityArtifactError`` fail-closed.
    """
    if path is not None:
        target = Path(path)
    else:
        candidates = _artifact_files(_ARTIFACT_DIR)
        if not candidates:
            raise CapabilityArtifactError(
                f"No transform capability artifact ({_ARTIFACT_GLOB}) found in {_ARTIFACT_DIR}. "
                f"Run data-transform-translation/scripts/build_artifact.py."
            )
        target = candidates[-1][1]

    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise CapabilityArtifactError(f"Capability artifact not found: {target}") from e
    except json.JSONDecodeError as e:
        raise CapabilityArtifactError(f"Capability artifact is not valid JSON: {target}: {e}") from e

    for required in ("schema_version", "checksum", "platforms", "portable_functions", "functions", "ops"):
        if required not in raw:
            raise CapabilityArtifactError(f"Capability artifact {target} missing key {required!r}.")

    expected = _canonical_checksum(raw)
    if raw["checksum"] != expected:
        raise CapabilityArtifactError(
            f"Capability artifact {target} checksum mismatch: stored {raw['checksum']} != "
            f"recomputed {expected}. The file was hand-edited or truncated — rebuild it."
        )

    return TransformCapabilities(
        schema_version=raw["schema_version"],
        checksum=raw["checksum"],
        platforms=tuple(raw["platforms"]),
        portable_functions=frozenset(f.upper() for f in raw["portable_functions"]),
        functions=raw["functions"],
        ops=raw["ops"],
        source_path=target,
    )


_CACHE: Optional[TransformCapabilities] = None


def get_capabilities(reload: bool = False) -> TransformCapabilities:
    """Process-cached accessor for the packaged artifact."""
    global _CACHE
    if _CACHE is None or reload:
        _CACHE = load_capabilities()
    return _CACHE
