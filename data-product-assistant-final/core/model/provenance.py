"""Per-field provenance and the regeneration merge policy.

The predecessors both destroyed user work on regeneration: an edit was flattened
to prose, fed back to the model, and whatever came out replaced everything.
Provenance exists so regeneration can be non-destructive.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class Origin(str, Enum):
    PACK_ENTITY = "PACK_ENTITY"      # from a domain pack framework entity
    PACK_BLOCK = "PACK_BLOCK"        # from a shared attribute block
    LLM = "LLM"                      # model-generated
    HUMAN = "HUMAN"                  # authored or edited by a user
    LIVE_CATALOG = "LIVE_CATALOG"    # read from live BigQuery — ground truth


#: Origins a regeneration must not overwrite. HUMAN is intent; LIVE_CATALOG is fact.
PROTECTED_ORIGINS = frozenset({Origin.HUMAN, Origin.LIVE_CATALOG})


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Provenance:
    origin: Origin
    locked: bool = False
    detail: str | None = None          # e.g. "packs/banking/retail_banking.json#slv_account"
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)
    updated_by: str | None = None

    @property
    def is_protected(self) -> bool:
        return self.locked or self.origin in PROTECTED_ORIGINS

    def touched_by_human(self, who: str | None = None) -> Provenance:
        return Provenance(
            origin=Origin.HUMAN,
            locked=True,
            detail=self.detail,
            created_at=self.created_at,
            updated_at=_now(),
            updated_by=who,
        )


@dataclass
class Suggestion:
    """A change an agent wanted to make to a protected field.

    Withheld rather than applied, and surfaced for review. An agent does not win
    by default against a human edit or against live catalog truth.
    """

    path: str
    current: Any
    proposed: Any
    rationale: str | None = None
    origin: Origin = Origin.LLM


@dataclass
class MergeReport:
    preserved: list[str] = field(default_factory=list)
    replaced: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    withheld: list[Suggestion] = field(default_factory=list)

    @property
    def is_noop(self) -> bool:
        return not (self.replaced or self.added or self.removed)

    def summary(self) -> str:
        return (
            f"{len(self.added)} added, {len(self.replaced)} replaced, "
            f"{len(self.removed)} removed, {len(self.preserved)} preserved, "
            f"{len(self.withheld)} suggestions withheld"
        )
