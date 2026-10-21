"""Composite-derivation catalog + run-scoped LLM proposals for feasibility.

Round 3 gives the feasibility matcher a way to satisfy a spec attribute that has
NO single-column counterpart by **composing** two or more columns of the SAME
table (e.g. ``name = concat(first_name, last_name)``). Two artifacts feed that:

1. A curated, GLOBAL catalog of :class:`DerivationPattern` — an alias-structured
   description of a target attribute, a **neutral operator** (naming the semantic
   intent, NOT executable SQL), and the component roles that compose it. The
   catalog is vendored inside the ``data-product-derivation-advisor`` skill so it
   ships + versions with the skill (like the feasibility corpus / code-migration
   SME corpora). Loaded **deterministically** here — offline/test-safe.
2. Run-scoped :class:`ProposedDerivation` rows the SAME skill emits (gaps-only),
   validated exact-ref against the spec's supplied inventory before they can ever
   be applied — no fuzzy re-match, no cross-spec leakage.

Fail-closed, precisely (mirrors :mod:`feasibility_spec`): a PRESENT but malformed
catalog raises :class:`DerivationCatalogError` — one bad entry rejects the WHOLE
catalog and surfaces a visible warning, never silently behaving as "no patterns".
A genuinely absent catalog file degrades to direct-only matching (an empty list).
"""
from __future__ import annotations

import re
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

from . import config
from .feasibility_spec import DerivationKind

DERIVATION_SCHEMA_VERSION = "1.0"

# Vendored inside the skill so it ships + versions with it.
CATALOG_DIR = config.SKILLS_DIR / "data-product-derivation-advisor" / "reference"
PATTERNS_FILE = CATALOG_DIR / "derivation_patterns.yaml"
MANIFEST_FILE = CATALOG_DIR / "manifest.yaml"


class DerivationCatalogError(ValueError):
    """Raised when the curated derivation catalog (or a supplied entry) does not
    validate. Fail-closed — one malformed entry rejects the WHOLE catalog and is
    surfaced to the caller, never swallowed into a silent empty list."""


# ── name/alias normalization ──────────────────────────────────────────────────

def norm_alias(text: str) -> str:
    """Normalize an identifier/alias for EQUALITY comparison: lowercase, split
    camelCase, collapse every non-alphanumeric run to one space, strip. So
    ``full name`` / ``full_name`` / ``fullName`` / ``Full Name`` compare equal.

    Deliberately equality (not fuzzy) — a curated alias is an exact-intent claim;
    fuzzy alias matching would re-introduce the wrong-entity ambiguity R3 fixes."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text or "")
    spaced = re.sub(r"[^A-Za-z0-9]+", " ", spaced)
    return " ".join(spaced.lower().split())


# ── curated pattern model (global) ─────────────────────────────────────────────

class ComponentSpec(BaseModel):
    """One composing role of a curated pattern.

    ``aliases`` are the candidate identifiers a source column may carry to fill the
    role (alias-equality primary; ``schema_dna`` similarity secondary). ``type_family``
    (optional) gates a ``compute`` kind, e.g. ``age_from_dob``'s ``birth_date`` role
    must resolve to a temporal column."""
    role: str
    aliases: list[str] = Field(default_factory=list)
    type_family: str = ""

    @field_validator("role")
    @classmethod
    def _role_nonempty(cls, v: str) -> str:
        if not (v or "").strip():
            raise ValueError("component role must be non-empty")
        return v


class DerivationPattern(BaseModel):
    """A curated, GLOBAL composition pattern.

    ``target_aliases`` name the spec attribute this composes (alias-equality); the
    ``operator`` names the neutral semantic intent (``join_with_separator`` /
    ``format_address`` / ``age_in_completed_years`` — NOT executable SQL); the
    ``components`` are the roles that compose it."""
    id: str
    target_aliases: list[str]
    kind: DerivationKind
    operator: str = ""
    separator: str = ""
    components: list[ComponentSpec]
    note: str = ""

    @field_validator("id")
    @classmethod
    def _id_nonempty(cls, v: str) -> str:
        if not (v or "").strip():
            raise ValueError("pattern id must be non-empty")
        return v

    @field_validator("target_aliases")
    @classmethod
    def _aliases_nonempty(cls, v: list) -> list:
        if not v:
            raise ValueError("pattern must declare at least one target alias")
        return v

    @field_validator("components")
    @classmethod
    def _components_min(cls, v: list) -> list:
        if len(v) < 1:
            raise ValueError("pattern must declare at least one component")
        return v

    def matches_attribute(self, *names: str) -> bool:
        """True iff any supplied attribute name/concept alias-equals a target alias."""
        targets = {norm_alias(a) for a in self.target_aliases}
        return any(norm_alias(n) in targets for n in names if n)


# ── run-scoped LLM proposal model ──────────────────────────────────────────────

class ProposedDerivation(BaseModel):
    """A run-scoped LLM proposal (gaps-only). Applied only after exact-ref
    validation against THAT spec's supplied inventory."""
    spec_id: str
    attribute_id: str
    kind: DerivationKind
    table_ref: str
    components: list[dict[str, str]] = Field(default_factory=list)  # [{role, column_ref}]
    separator: str = ""
    operator: str = ""
    rationale: str = ""

    @field_validator("table_ref")
    @classmethod
    def _table_nonempty(cls, v: str) -> str:
        if not (v or "").strip():
            raise ValueError("table_ref must be non-empty")
        return v

    @property
    def stable_id(self) -> str:
        """Deterministic id synthesized from the scoped fields (spec + attr + table
        + sorted component columns) — stable across re-parses of the same proposal."""
        cols = ",".join(sorted((c.get("column_ref") or "") for c in self.components))
        return f"proposal:{self.spec_id}|{self.attribute_id}|{self.table_ref}|{cols}"


# ── parsing (fail-closed) ──────────────────────────────────────────────────────

def parse_pattern(data: dict[str, Any]) -> DerivationPattern:
    if not isinstance(data, dict):
        raise DerivationCatalogError("pattern must be a mapping")
    try:
        return DerivationPattern.model_validate(data)
    except ValidationError as e:
        raise DerivationCatalogError(str(e)) from e


def parse_patterns(entries: Any) -> list[DerivationPattern]:
    """Validate a list of raw pattern dicts. One bad entry (or a duplicate id)
    rejects the WHOLE list (fail-closed)."""
    if not isinstance(entries, list):
        raise DerivationCatalogError("patterns must be a list")
    out: list[DerivationPattern] = []
    seen: set[str] = set()
    for i, entry in enumerate(entries):
        try:
            pat = parse_pattern(entry)
        except DerivationCatalogError as e:
            raise DerivationCatalogError(f"pattern #{i}: {e}") from e
        if pat.id in seen:
            raise DerivationCatalogError(f"duplicate pattern id '{pat.id}'")
        seen.add(pat.id)
        out.append(pat)
    return out


def parse_proposal(data: dict[str, Any]) -> ProposedDerivation:
    """Validate one LLM proposal dict into a typed :class:`ProposedDerivation`
    (fail-closed at the field level; callers still exact-ref it before applying)."""
    if not isinstance(data, dict):
        raise DerivationCatalogError("proposal must be a mapping")
    try:
        return ProposedDerivation.model_validate(data)
    except ValidationError as e:
        raise DerivationCatalogError(str(e)) from e


# ── run-scoped reasoning-matcher decision model ────────────────────────────────

class MatchDecision(BaseModel):
    """A run-scoped **reasoning column-matcher** decision for ONE uncertain spec
    attribute (Phase 2). The LLM reasons over the estate's generated descriptions
    and either re-points the attribute to one of the columns it was SHOWN
    (``decision="match"``) or declares it a genuine gap (``decision="gap"``).

    Applied only after exact-ref validation against the candidates shown for that
    attribute — the LLM can never invent a column, only choose a shown one or gap."""
    spec_id: str
    attribute_id: str
    decision: str  # "match" | "gap"
    column_ref: str = ""
    table_ref: str = ""
    confidence: float = 0.0
    reason: str = ""

    @field_validator("spec_id", "attribute_id")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        if not (v or "").strip():
            raise ValueError("spec_id and attribute_id are required")
        return v

    @field_validator("decision")
    @classmethod
    def _decision_valid(cls, v: str) -> str:
        if v not in ("match", "gap"):
            raise ValueError("decision must be 'match' or 'gap'")
        return v


def parse_match(data: dict[str, Any]) -> MatchDecision:
    """Validate one LLM match decision into a typed :class:`MatchDecision`
    (fail-closed; callers still exact-ref a ``match`` against the shown candidates)."""
    if not isinstance(data, dict):
        raise DerivationCatalogError("match decision must be a mapping")
    try:
        return MatchDecision.model_validate(data)
    except ValidationError as e:
        raise DerivationCatalogError(str(e)) from e


# ── catalog manifest + loader ──────────────────────────────────────────────────

def _manifest() -> dict[str, Any]:
    try:
        return yaml.safe_load(MANIFEST_FILE.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}


def catalog_version() -> str:
    """The catalog manifest's ``catalog_version`` (``"0"`` when absent)."""
    return str(_manifest().get("catalog_version") or "0")


def schema_version() -> str:
    """The catalog manifest's ``schema_version`` (defaults to the code version)."""
    return str(_manifest().get("schema_version") or DERIVATION_SCHEMA_VERSION)


def load_patterns() -> list[DerivationPattern]:
    """Load + validate the curated global derivation catalog (fail-closed).

    A genuinely absent catalog file → ``[]`` (the feature degrades to direct-only
    matching). A PRESENT but malformed catalog raises :class:`DerivationCatalogError`
    — never a silent empty list. Callers in the run path catch it and stamp
    ``derivation_catalog_error`` on the run summary."""
    if not PATTERNS_FILE.exists():
        return []
    try:
        data = yaml.safe_load(PATTERNS_FILE.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        raise DerivationCatalogError(f"{PATTERNS_FILE.name}: {e}") from e
    entries = data.get("patterns") if isinstance(data, dict) else None
    if entries is None:
        return []
    return parse_patterns(entries)
