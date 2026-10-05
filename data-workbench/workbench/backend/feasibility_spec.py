"""The reference data-product **feasibility spec** contract + vendored corpus.

A ``FeasibilitySpec`` is the enriched, buildability-aware description of a desired
data product (per domain). It goes well beyond name/type/concept — buildability
needs per-attribute required-vs-optional, grain + keys, freshness/history
expectations, allowed derivations, composition/join requirements, and
security/governance classification. The corpus is a **vendored skill corpus**
under ``workbench-skills/skills/data-product-feasibility-evaluator/reference/``,
version-controlled and corpus-versioned. It is a GENERATED artifact of the
Blueprint Library: ``template_corpus.regenerate_feasibility_corpus`` rewrites it
from every PUBLISHED template (at bootstrap + on each template publish).

The Pydantic models here are **fail-closed** (mirrors
``intake_blueprint.parse_blueprint``): a corpus file that doesn't validate raises
``FeasibilitySpecValidationError`` rather than silently loading a half-spec.
"""
from __future__ import annotations

import re
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

from . import config

SPEC_SCHEMA_VERSION = "1.0"

# The vendored corpus lives inside the evaluator skill so it ships + versions with
# the skill (like the code-migration SME corpora). ``reference/<domain>/<spec>.yaml``
# + a top-level ``reference/corpus.yaml`` manifest carrying ``corpus_version``.
CORPUS_DIR = (
    config.SKILLS_DIR / "data-product-feasibility-evaluator" / "reference"
)
CORPUS_MANIFEST = CORPUS_DIR / "corpus.yaml"
INDEX_FILE = CORPUS_DIR / "index.yaml"


# ── enums ────────────────────────────────────────────────────────────────────

class ProductKind(str, Enum):
    source = "source"
    aggregate = "aggregate"
    consumer = "consumer"


class Classification(str, Enum):
    public = "public"
    internal = "internal"
    confidential = "confidential"
    pii = "pii"
    restricted = "restricted"


class History(str, Enum):
    current = "current"       # latest state only
    snapshot = "snapshot"     # point-in-time snapshots
    scd2 = "scd2"             # full effective-dated history


class DerivationKind(str, Enum):
    """Transformations the evaluator may treat as satisfying a spec attribute that
    isn't present verbatim in a candidate. Deliberately coarse — grounds the
    ``adaptable`` tier without over-promising. (Note: coarse→fine **aggregation**
    is allowed; fine-from-coarse expansion is NOT — a spec that needs finer grain
    than the estate offers is a genuine gap, not a derivation.)"""

    rename = "rename"
    cast = "cast"
    currency_normalize = "currency_normalize"
    unit_convert = "unit_convert"
    aggregate = "aggregate"           # coarser grain (weekly→monthly)
    concat = "concat"
    lookup = "lookup"
    bucket = "bucket"
    mask = "mask"
    compute = "compute"               # generic arithmetic/expression


# ── nested shapes ────────────────────────────────────────────────────────────

class SpecAttribute(BaseModel):
    name: str
    type: str = ""
    concept: str = ""
    required: bool = True
    is_key: bool = False
    classification: Classification = Classification.internal
    # Derivations that, applied to a near-match candidate column, satisfy THIS
    # attribute (e.g. a rename, a currency_normalize). Empty = must match ~directly.
    derivations: list[DerivationKind] = Field(default_factory=list)
    note: str = ""
    description: str = ""

    @field_validator("name")
    @classmethod
    def _name_nonempty(cls, v: str) -> str:
        if not (v or "").strip():
            raise ValueError("attribute name must be non-empty")
        return v

    @field_validator("note", "concept", "type", "description", mode="before")
    @classmethod
    def _none_to_empty(cls, v: Any) -> str:
        return "" if v is None else v


class GrainSpec(BaseModel):
    """The grain the product is expected at, expressed as its key columns."""
    keys: list[str] = Field(default_factory=list)
    description: str = ""


class FreshnessSpec(BaseModel):
    history: History = History.current
    cadence: str = ""  # e.g. "daily", "monthly" — informational
    description: str = ""


class CompositionSpec(BaseModel):
    """How the product may be assembled from multiple sources. ``join_keys`` are
    the shared identity columns a joinability check confirms the estate can join
    on; ``composed_of`` names composing reference spec ids (aggregated products)."""
    join_keys: list[str] = Field(default_factory=list)
    composed_of: list[str] = Field(default_factory=list)
    description: str = ""


class FeasibilitySpec(BaseModel):
    spec_id: str
    name: str
    domain: str
    product_kind: ProductKind = ProductKind.consumer
    description: str = ""
    schema_version: str = SPEC_SCHEMA_VERSION
    # Stamped by the loader from the corpus manifest — not authored per file.
    corpus_version: str = ""
    attributes: list[SpecAttribute]
    grain: GrainSpec = Field(default_factory=GrainSpec)
    freshness: FreshnessSpec = Field(default_factory=FreshnessSpec)
    composition: CompositionSpec = Field(default_factory=CompositionSpec)
    # Spec-level allowed derivations (in addition to per-attribute ones).
    allowed_derivations: list[DerivationKind] = Field(default_factory=list)
    classification: Classification = Classification.internal

    @field_validator("spec_id", "name", "domain")
    @classmethod
    def _required_nonempty(cls, v: str) -> str:
        if not (v or "").strip():
            raise ValueError("spec_id, name and domain are required")
        return v

    @field_validator("attributes")
    @classmethod
    def _at_least_one_attr(cls, v: list) -> list:
        if not v:
            raise ValueError("a feasibility spec must declare at least one attribute")
        return v

    # ── derived helpers ──────────────────────────────────────────────────────
    @property
    def required_attributes(self) -> list[SpecAttribute]:
        return [a for a in self.attributes if a.required]

    def allowed_derivations_for(self, attr: SpecAttribute) -> set[DerivationKind]:
        return set(attr.derivations) | set(self.allowed_derivations)


class FeasibilitySpecValidationError(ValueError):
    """Raised when a corpus file / spec dict does not conform to the schema.

    Callers surface this as an operator-facing corpus error — we never
    default-fill a malformed spec into a half-populated shape."""


def parse_spec(data: dict[str, Any]) -> FeasibilitySpec:
    """Validate a raw dict into a typed :class:`FeasibilitySpec` (fail-closed)."""
    if not isinstance(data, dict):
        raise FeasibilitySpecValidationError("spec must be a JSON/YAML object")
    try:
        return FeasibilitySpec.model_validate(data)
    except ValidationError as e:
        raise FeasibilitySpecValidationError(str(e)) from e


def slug(text: str, fallback: str = "x") -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return s or fallback


# ── corpus loading ───────────────────────────────────────────────────────────

def corpus_version() -> str:
    """Read the corpus manifest's ``corpus_version`` (``"0"`` when absent)."""
    try:
        manifest = yaml.safe_load(CORPUS_MANIFEST.read_text(encoding="utf-8")) or {}
        return str(manifest.get("corpus_version") or "0")
    except (OSError, yaml.YAMLError):
        return "0"


def _manifest() -> dict[str, Any]:
    try:
        return yaml.safe_load(CORPUS_MANIFEST.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}


def list_domains() -> list[str]:
    """Domain names present in the corpus (directory names → display names via
    the manifest, else the raw subdir names)."""
    if not CORPUS_DIR.is_dir():
        return []
    manifest = _manifest()
    display = {slug(d): d for d in (manifest.get("domains") or [])}
    out = []
    for child in sorted(CORPUS_DIR.iterdir()):
        if child.is_dir():
            out.append(display.get(child.name, child.name))
    return out


def load_index(domain: Optional[str] = None) -> list[dict[str, Any]]:
    """Load the pre-built lightweight spec index (fast path for the UI picker).

    Falls back to deriving from the full corpus if ``index.yaml`` is absent.
    """
    try:
        data = yaml.safe_load(INDEX_FILE.read_text(encoding="utf-8")) or {}
        entries: list[dict[str, Any]] = data.get("specs") or []
    except (OSError, yaml.YAMLError):
        entries = [
            {"spec_id": s.spec_id, "name": s.name, "domain": s.domain,
             "product_kind": s.product_kind.value, "description": s.description,
             "required_count": len(s.required_attributes), "total_count": len(s.attributes)}
            for s in load_corpus()
        ]
    if domain:
        want = slug(domain)
        entries = [e for e in entries if slug(e.get("domain", "")) == want]
    return entries


def load_corpus(domain: Optional[str] = None) -> list[FeasibilitySpec]:
    """Load + validate every spec in the corpus (optionally one domain).

    Each ``reference/<domain-slug>/<spec>.yaml`` is parsed fail-closed and stamped
    with the manifest ``corpus_version``. A malformed file raises
    :class:`FeasibilitySpecValidationError` naming the file — the corpus is a
    first-class artifact, not best-effort input.
    """
    if not CORPUS_DIR.is_dir():
        return []
    version = corpus_version()
    want_slug = slug(domain) if domain else None
    specs: list[FeasibilitySpec] = []
    for domain_dir in sorted(CORPUS_DIR.iterdir()):
        if not domain_dir.is_dir():
            continue
        if want_slug and domain_dir.name != want_slug:
            continue
        for spec_file in sorted(domain_dir.glob("*.yaml")):
            try:
                data = yaml.safe_load(spec_file.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError) as e:
                raise FeasibilitySpecValidationError(f"{spec_file.name}: {e}") from e
            try:
                spec = parse_spec(data)
            except FeasibilitySpecValidationError as e:
                raise FeasibilitySpecValidationError(f"{spec_file.name}: {e}") from e
            spec.corpus_version = version
            specs.append(spec)
    return specs
