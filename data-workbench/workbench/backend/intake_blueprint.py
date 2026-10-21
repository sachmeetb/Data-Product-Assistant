"""Inbound-intake scaffold **blueprint** — the strict internal contract.

The external submission *envelope* is deliberately loose (free-form
``content[]`` parts). This module is the opposite: the strict, versioned shape
that the isolated parser must emit and that the review UI + scaffold saga
consume. Keeping this contract strict from day one (not deferred) is what makes
the untrusted-input parser safe — parser output is validated against these
models and a structural mismatch becomes ``parse_failed`` rather than a
half-populated scaffold.

Design points (from the hardened plan):
  • ``Blueprint`` is a Pydantic **discriminated union** on ``scenario`` —
    ``MigrationBlueprint | ModernizationBlueprint``.
  • Every graded scalar is a :class:`ConfidenceField` carrying a confidence
    band + a short ``why`` + a per-field review state, so the UI can separate
    "accept as-is" (high) from "please confirm" (ambiguous/low/missing).
  • Every candidate (dataset / product / dependency) carries a **stable
    candidate id** so edits, dependencies, and spawn records reference ids —
    never names. :func:`normalize_ids` fills any the parser omitted, but does
    NOT invent field *values* (no default-fill of required scaffold data).
  • ``blueprint_version`` is stamped so a future schema revision can migrate.
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Annotated, Any, Generic, Literal, Optional, TypeVar, Union

from pydantic import BaseModel, Field, TypeAdapter, ValidationError, field_validator

BLUEPRINT_VERSION = "1.0"

T = TypeVar("T")


class Confidence(str, Enum):
    high = "high"
    medium = "medium"
    low = "low"
    missing = "missing"


class ReviewState(str, Enum):
    suggested = "suggested"   # parser's proposal, not yet touched
    confirmed = "confirmed"   # practitioner accepted as-is
    edited = "edited"         # practitioner changed the value
    excluded = "excluded"     # practitioner dropped this candidate from scaffold


class ConfidenceField(BaseModel, Generic[T]):
    """A single graded value: the proposed ``value`` + how sure the parser is."""

    value: Optional[T] = None
    confidence: Confidence = Confidence.missing
    why: str = ""
    review_state: ReviewState = ReviewState.suggested


def _cf() -> "ConfidenceField[Any]":
    """Default-factory for an empty (missing) ConfidenceField."""
    return ConfidenceField()


class Gap(BaseModel):
    """A field the parser could not resolve — surfaced for the practitioner to
    fill before the scaffold is allowed to proceed."""

    field: str
    why: str = ""


# ── shared candidate shapes ──────────────────────────────────────────────────

class ColumnCandidate(BaseModel):
    candidate_id: Optional[str] = None
    name: ConfidenceField[str]
    data_type: ConfidenceField[str] = Field(default_factory=_cf)
    note: str = ""

    @field_validator("note", mode="before")
    @classmethod
    def _note_none_to_empty(cls, v: Any) -> str:
        # The parser reasonably emits note: null for "no note" — coerce to "".
        return "" if v is None else v


class DatasetCandidate(BaseModel):
    candidate_id: Optional[str] = None
    name: ConfidenceField[str]
    columns: list[ColumnCandidate] = Field(default_factory=list)
    incremental_cursor: ConfidenceField[str] = Field(default_factory=_cf)
    review_state: ReviewState = ReviewState.suggested


# ── migration ────────────────────────────────────────────────────────────────

class MigrationBlueprint(BaseModel):
    scenario: Literal["migration"] = "migration"
    blueprint_version: str = BLUEPRINT_VERSION
    overall_confidence: Confidence = Confidence.missing
    # project_name is the one genuinely required field — a migration scaffold
    # with no product name is meaningless, so its absence is a parse failure.
    project_name: ConfidenceField[str]
    domain: ConfidenceField[str] = Field(default_factory=_cf)
    source_platform: ConfidenceField[str] = Field(default_factory=_cf)
    target_platform: ConfidenceField[str] = Field(default_factory=_cf)
    write_disposition: ConfidenceField[str] = Field(default_factory=_cf)
    datasets: list[DatasetCandidate] = Field(default_factory=list)
    gaps: list[Gap] = Field(default_factory=list)
    rationale: str = ""


# ── modernization ──────────────────────────────────────────────────────────

class ProductCandidate(BaseModel):
    candidate_id: Optional[str] = None
    name: ConfidenceField[str]
    domain: ConfidenceField[str] = Field(default_factory=_cf)
    description: str = ""
    product_idea: str = ""          # source-aligned: free-form prose for discovery
    purpose: str = ""               # consumer-aligned: purpose statement
    odcs: Optional[dict[str, Any]] = None   # optional embedded ODCS draft
    datasets: list[DatasetCandidate] = Field(default_factory=list)
    confidence: Confidence = Confidence.missing
    review_state: ReviewState = ReviewState.suggested


class Dependency(BaseModel):
    """A consumer→source dependency, keyed by stable candidate ids (never names).

    Exactly one of ``to_candidate_id`` (a NEW source-aligned candidate in this
    same blueprint) or ``to_external_uri`` (an already-published source product)
    identifies the target.
    """

    dependency_id: Optional[str] = None
    from_candidate_id: str
    to_candidate_id: Optional[str] = None
    to_external_uri: Optional[str] = None
    confidence: Confidence = Confidence.missing
    review_state: ReviewState = ReviewState.suggested


class ModernizationBlueprint(BaseModel):
    scenario: Literal["modernization"] = "modernization"
    blueprint_version: str = BLUEPRINT_VERSION
    overall_confidence: Confidence = Confidence.missing
    source_aligned: list[ProductCandidate] = Field(default_factory=list)
    consumer_aligned: list[ProductCandidate] = Field(default_factory=list)
    dependencies: list[Dependency] = Field(default_factory=list)
    gaps: list[Gap] = Field(default_factory=list)
    rationale: str = ""


Blueprint = Annotated[
    Union[MigrationBlueprint, ModernizationBlueprint],
    Field(discriminator="scenario"),
]

_ADAPTER: TypeAdapter[Any] = TypeAdapter(Blueprint)


class BlueprintValidationError(ValueError):
    """Raised when raw parser output does not conform to the blueprint schema.

    Callers turn this into an ``IntakeSubmission.status='parse_failed'`` — we do
    NOT default-fill required scaffold fields to paper over a bad parse.
    """


def _slug(text: str, fallback: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return s or fallback


def parse_blueprint(data: dict[str, Any]) -> Union[MigrationBlueprint, ModernizationBlueprint]:
    """Validate a raw dict (parser output) into a typed Blueprint.

    Raises :class:`BlueprintValidationError` on any structural mismatch —
    including a missing/unknown ``scenario`` discriminator or a missing required
    field. The message is safe to surface in ``parse_meta_json``.
    """
    if not isinstance(data, dict):
        raise BlueprintValidationError("blueprint must be a JSON object")
    try:
        return _ADAPTER.validate_python(data)
    except ValidationError as e:
        raise BlueprintValidationError(str(e)) from e


def normalize_ids(bp: Union[MigrationBlueprint, ModernizationBlueprint]) -> None:
    """Fill any candidate/dependency ids the parser omitted, deterministically.

    Structural only — assigns stable ids so downstream edits/spawns can
    reference them; never invents field *values*. Mutates ``bp`` in place. Ids
    the parser already supplied are preserved (so its own dependency references
    stay valid).
    """
    if isinstance(bp, MigrationBlueprint):
        for i, ds in enumerate(bp.datasets):
            if not ds.candidate_id:
                ds.candidate_id = f"ds-{i}-{_slug(ds.name.value or '', str(i))}"
            for j, col in enumerate(ds.columns):
                if not col.candidate_id:
                    col.candidate_id = f"{ds.candidate_id}.col-{j}-{_slug(col.name.value or '', str(j))}"
        return

    for i, p in enumerate(bp.source_aligned):
        if not p.candidate_id:
            p.candidate_id = f"src-{i}-{_slug(p.name.value or '', str(i))}"
    for i, p in enumerate(bp.consumer_aligned):
        if not p.candidate_id:
            p.candidate_id = f"con-{i}-{_slug(p.name.value or '', str(i))}"
    for i, dep in enumerate(bp.dependencies):
        if not dep.dependency_id:
            dep.dependency_id = f"dep-{i}"
