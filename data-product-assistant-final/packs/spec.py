"""Domain pack loading and detection.

A pack is data and metadata -- no code. Adding an industry means adding a pack,
never editing core. Ports the live `gcs_registry.json` mechanism from the GOLD
product (its `domain_registry.json` was never read by anything and is dropped).
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from core.model.provenance import Origin, Provenance
from core.model.tree import Attribute, Domain, domain_from_framework
from core.model.types import CanonicalType, normalize

_PACKS_DIR = Path(__file__).parent
_REGISTRY_PATH = _PACKS_DIR / "registry.json"
_BLOCKS_DIR = _PACKS_DIR / "_blocks"

#: How many detection signals must hit before a domain is considered a match.
DETECTION_THRESHOLD = 2

_MAX_REF_DEPTH = 5

_JSON_SCHEMA_TYPES = {
    "string": CanonicalType.STRING,
    "integer": CanonicalType.INT,
    "number": CanonicalType.FLOAT,
    "boolean": CanonicalType.BOOL,
    "object": CanonicalType.STRUCT,
    "array": CanonicalType.ARRAY,
}

_JSON_SCHEMA_FORMATS = {
    "date": CanonicalType.DATE,
    "date-time": CanonicalType.TIMESTAMP,
    "time": CanonicalType.TIME,
}


def _ref_to_block(ref: str) -> str:
    """`bfsi.silver/common/money.yaml` or `money.yaml` -> `money`."""
    tail = ref.replace("\\", "/").rstrip("/").split("/")[-1]
    return tail[:-5] if tail.endswith(".yaml") else tail


def _resolve_type(spec: dict, *, block: str, field: str) -> tuple[CanonicalType, bool]:
    """Map a JSON Schema property to a canonical type.

    Returns (type, nullable_from_union). Raises rather than defaulting: a
    silently guessed type is how `DOUBLE` reached BigQuery-targeted frameworks.
    """
    declared = spec.get("type")
    nullable = False

    if isinstance(declared, list):
        nullable = "null" in declared
        non_null = [t for t in declared if t != "null"]
        declared = non_null[0] if non_null else None

    override = spec.get("x-canonical-type")
    if override:
        return normalize(override), nullable

    fmt = spec.get("format")
    if fmt in _JSON_SCHEMA_FORMATS:
        return _JSON_SCHEMA_FORMATS[fmt], nullable

    if spec.get("enum") is not None and declared is None:
        return CanonicalType.STRING, nullable

    if declared in _JSON_SCHEMA_TYPES:
        return _JSON_SCHEMA_TYPES[declared], nullable

    raise PackNotFoundError(
        f"block {block!r} field {field!r} declares unmappable type {declared!r}; "
        "add an x-canonical-type annotation"
    )


@dataclass
class PackEntry:
    industry: str
    domain: str
    path: str
    detection_signals: list[str] = field(default_factory=list)
    standards: list[str] = field(default_factory=list)
    display_name: str = ""

    @property
    def key(self) -> str:
        return f"{self.industry}.{self.domain}"


@dataclass
class DetectionHit:
    entry: PackEntry
    score: int
    matched: list[str]


class PackNotFoundError(FileNotFoundError):
    pass


class PackRegistry:
    def __init__(self, registry_path: Path | None = None) -> None:
        self._path = registry_path or _REGISTRY_PATH
        self._entries: list[PackEntry] | None = None
        self._domain_cache: dict[str, Domain] = {}

    def entries(self) -> list[PackEntry]:
        if self._entries is not None:
            return self._entries

        with open(self._path, encoding="utf-8") as f:
            raw = json.load(f)

        out: list[PackEntry] = []
        for industry, domains in (raw.get("industries") or {}).items():
            for domain, spec in domains.items():
                out.append(
                    PackEntry(
                        industry=industry,
                        domain=domain,
                        path=spec["path"],
                        detection_signals=list(spec.get("detection_signals", [])),
                        standards=list(spec.get("standards", [])),
                        display_name=spec.get("display_name", ""),
                    )
                )
        self._entries = out
        return out

    def entry(self, key: str) -> PackEntry:
        for e in self.entries():
            if e.key == key or e.domain == key:
                return e
        raise PackNotFoundError(f"no pack entry for {key!r}")

    def detect(self, corpus: str) -> list[DetectionHit]:
        """Score every entry against a lowercased corpus, best first.

        Returns all qualifying hits rather than a single winner -- a cross-domain
        product needs to be able to offer several.
        """
        lowered = corpus.lower()
        hits: list[DetectionHit] = []

        for entry in self.entries():
            matched = [s for s in entry.detection_signals if s.lower() in lowered]
            if len(matched) >= DETECTION_THRESHOLD:
                hits.append(
                    DetectionHit(entry=entry, score=len(matched), matched=matched)
                )

        return sorted(hits, key=lambda h: h.score, reverse=True)

    def load_domain(self, key: str) -> Domain:
        """Instantiate a domain from its pack.

        Returns a deep copy: the cache holds an immutable template, while callers
        get an instance they own. A design mutates its domains (pulling in catalog
        columns, locking edits), so sharing the cached object would let one
        session corrupt another's.
        """
        entry = self.entry(key)

        if entry.key not in self._domain_cache:
            path = _PACKS_DIR / entry.path
            if not path.exists():
                raise PackNotFoundError(
                    f"pack {entry.key} declares {entry.path}, which does not exist"
                )
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            self._domain_cache[entry.key] = domain_from_framework(
                raw, pack_ref=entry.path, industry=entry.industry
            )

        return deepcopy(self._domain_cache[entry.key])


class BlockLibrary:
    """Shared, industry-neutral attribute blocks.

    These came from the SILVER product's `common/` directory but are not
    BFSI-specific: money, identifier, temporal, postal-address, contact-point,
    quantity, rate, code-value, party-name and technical-metadata apply to every
    industry, so they live in core rather than in the banking pack.
    """

    def __init__(self, blocks_dir: Path | None = None) -> None:
        self._dir = blocks_dir or _BLOCKS_DIR
        self._cache: dict[str, dict] = {}

    def names(self) -> list[str]:
        if not self._dir.exists():
            return []
        return sorted(p.stem for p in self._dir.glob("*.yaml"))

    def load(self, name: str) -> dict:
        if name in self._cache:
            return self._cache[name]
        path = self._dir / f"{name}.yaml"
        if not path.exists():
            raise PackNotFoundError(f"no block {name!r} in {self._dir}")
        with open(path, encoding="utf-8") as f:
            spec = yaml.safe_load(f) or {}
        self._cache[name] = spec
        return spec

    def defs(self, name: str) -> list[str]:
        """Reusable `$defs` a block exposes for other blocks to reference."""
        return sorted((self.load(name).get("$defs") or {}).keys())

    def is_definitions_only(self, name: str) -> bool:
        """True for library blocks with no top-level properties (e.g. temporal)."""
        spec = self.load(name)
        return not spec.get("properties") and bool(spec.get("$defs"))

    def expand(
        self, name: str, *, prefix: str = "", _depth: int = 0
    ) -> list[Attribute]:
        """Flatten a block into concrete attributes, resolving `$ref`.

        Ports SILVER's `schema_loader._flatten_properties`, which resolved `$ref`
        into prefixed columns -- one of the few deterministic, code-based paths in
        either product.

        The blocks are JSON Schema draft 2020-12, so declared types describe the
        wire format. Where the warehouse type differs (money.amount is a string
        on the wire to preserve decimal precision) the intended type is annotated
        with `x-canonical-type`.

        A definitions-only block expands to nothing, by design.
        """
        return self._expand_schema(
            self.load(name), block=name, prefix=prefix, depth=_depth
        )

    def _expand_schema(
        self, spec: dict, *, block: str, prefix: str, depth: int
    ) -> list[Attribute]:
        if depth > _MAX_REF_DEPTH:
            raise PackNotFoundError(
                f"$ref nesting exceeded {_MAX_REF_DEPTH} expanding block {block!r}"
            )

        required = set(spec.get("required") or [])
        out: list[Attribute] = []

        for field_name, field_spec in (spec.get("properties") or {}).items():
            full = f"{prefix}{field_name}" if prefix else field_name
            optional_here = field_name not in required

            ref = field_spec.get("$ref")
            if ref:
                fragment, source_block = self._resolve_ref(ref, spec, block)

                if fragment.get("properties"):
                    out.extend(
                        self._expand_schema(
                            fragment,
                            block=source_block,
                            prefix=f"{full}_",
                            depth=depth + 1,
                        )
                    )
                    continue

                # A scalar $def (e.g. identifier's Scheme enum) is one column.
                field_spec = {**fragment, **{
                    k: v for k, v in field_spec.items() if k != "$ref"
                }}
                block_for_field = source_block
            else:
                block_for_field = block

            ctype, nullable_union = _resolve_type(
                field_spec, block=block_for_field, field=field_name
            )
            out.append(
                Attribute(
                    name=full,
                    canonical_type=ctype,
                    nullable=nullable_union or optional_here,
                    description=(field_spec.get("description") or "").strip(),
                    provenance=Provenance(
                        origin=Origin.PACK_BLOCK,
                        detail=f"_blocks/{block_for_field}.yaml",
                    ),
                    block=block_for_field,
                    standard_refs=list(field_spec.get("standard_refs", [])),
                )
            )

        return out

    def _resolve_ref(
        self, ref: str, current: dict, current_block: str
    ) -> tuple[dict, str]:
        """Resolve `#/$defs/X`, `other.yaml`, or `other.yaml#/$defs/X`."""
        file_part, _, pointer = ref.partition("#")

        if file_part:
            block = _ref_to_block(file_part)
            spec = self.load(block)
        else:
            block, spec = current_block, current

        if not pointer:
            return spec, block

        node: object = spec
        for segment in (s for s in pointer.split("/") if s):
            if not isinstance(node, dict) or segment not in node:
                raise PackNotFoundError(
                    f"block {current_block!r} has a $ref {ref!r} that does not resolve"
                )
            node = node[segment]

        if not isinstance(node, dict):
            raise PackNotFoundError(
                f"block {current_block!r} $ref {ref!r} resolved to a non-schema value"
            )
        return node, block
