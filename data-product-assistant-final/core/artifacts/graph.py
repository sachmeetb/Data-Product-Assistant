"""Artifact dependency DAG and selective invalidation.

Both predecessors stored artifacts as opaque blobs on a session dict and
regenerated everything on any change. Making the dependency order explicit lets
an edit compute its own blast radius: editing an attribute in the silver schema
invalidates the STTM, transformation, spec and SQL -- not discovery, and not the
requirement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ArtifactId(str, Enum):
    REQUIREMENT = "requirement"
    CLASSIFICATION = "classification"
    BRONZE_PROFILE = "bronze_profile"
    BRONZE_TABLES = "bronze_tables"
    DISCOVERY = "discovery"
    CHALLENGE = "challenge"
    DESIGN_TREE = "design_tree"          # the canonical Domain->Table->Attribute tree
    GOLD_ER = "gold_er"
    STTM = "sttm"
    PIPELINE_IR = "pipeline_ir"
    RENDERED_PIPELINE = "rendered_pipeline"
    VALIDATION = "validation"
    PUBLISHED = "published"
    CONTRACT = "contract"


#: Direct upstream dependencies. Edges point from an artifact to what it needs.
DEPENDS_ON: dict[ArtifactId, tuple[ArtifactId, ...]] = {
    ArtifactId.REQUIREMENT: (),
    ArtifactId.CLASSIFICATION: (ArtifactId.REQUIREMENT,),
    ArtifactId.BRONZE_PROFILE: (ArtifactId.REQUIREMENT,),
    ArtifactId.BRONZE_TABLES: (ArtifactId.BRONZE_PROFILE,),
    ArtifactId.DISCOVERY: (ArtifactId.CLASSIFICATION, ArtifactId.BRONZE_TABLES),
    ArtifactId.CHALLENGE: (ArtifactId.DISCOVERY,),
    ArtifactId.DESIGN_TREE: (ArtifactId.CHALLENGE,),
    ArtifactId.GOLD_ER: (ArtifactId.DESIGN_TREE,),
    ArtifactId.STTM: (ArtifactId.DESIGN_TREE, ArtifactId.GOLD_ER),
    ArtifactId.PIPELINE_IR: (ArtifactId.STTM,),
    ArtifactId.RENDERED_PIPELINE: (ArtifactId.PIPELINE_IR,),
    ArtifactId.VALIDATION: (ArtifactId.RENDERED_PIPELINE,),
    ArtifactId.PUBLISHED: (ArtifactId.VALIDATION,),
    ArtifactId.CONTRACT: (ArtifactId.DESIGN_TREE, ArtifactId.STTM),
}


class Staleness(str, Enum):
    FRESH = "FRESH"
    STALE = "STALE"          # an upstream artifact changed
    NOT_BUILT = "NOT_BUILT"


@dataclass
class ArtifactState:
    id: ArtifactId
    version: int = 0
    staleness: Staleness = Staleness.NOT_BUILT
    stale_because: list[ArtifactId] = field(default_factory=list)


def _dependents() -> dict[ArtifactId, set[ArtifactId]]:
    out: dict[ArtifactId, set[ArtifactId]] = {a: set() for a in ArtifactId}
    for artifact, upstreams in DEPENDS_ON.items():
        for up in upstreams:
            out[up].add(artifact)
    return out


DEPENDENTS = _dependents()


def downstream(changed: ArtifactId) -> list[ArtifactId]:
    """Every artifact transitively affected by a change, in dependency order."""
    seen: set[ArtifactId] = set()
    frontier = list(DEPENDENTS[changed])

    while frontier:
        current = frontier.pop()
        if current in seen:
            continue
        seen.add(current)
        frontier.extend(DEPENDENTS[current])

    return [a for a in _topological() if a in seen]


def _topological() -> list[ArtifactId]:
    remaining = {a: set(DEPENDS_ON.get(a, ())) for a in ArtifactId}
    out: list[ArtifactId] = []

    while remaining:
        ready = sorted(
            (a for a, deps in remaining.items() if not deps - set(out)),
            key=lambda a: a.value,
        )
        if not ready:
            raise RuntimeError(f"dependency cycle among {sorted(r.value for r in remaining)}")
        for a in ready:
            out.append(a)
            del remaining[a]

    return out


class ArtifactGraph:
    """Tracks per-artifact version and staleness for one session."""

    def __init__(self) -> None:
        self._state: dict[ArtifactId, ArtifactState] = {
            a: ArtifactState(id=a) for a in ArtifactId
        }

    def state(self, artifact: ArtifactId) -> ArtifactState:
        return self._state[artifact]

    def mark_built(self, artifact: ArtifactId) -> ArtifactState:
        st = self._state[artifact]
        st.version += 1
        st.staleness = Staleness.FRESH
        st.stale_because = []
        return st

    def mark_changed(self, artifact: ArtifactId) -> list[ArtifactId]:
        """Record a change and cascade staleness. Returns what became stale."""
        self.mark_built(artifact)

        affected = downstream(artifact)
        for dep in affected:
            st = self._state[dep]
            if st.staleness is Staleness.NOT_BUILT:
                continue
            st.staleness = Staleness.STALE
            if artifact not in st.stale_because:
                st.stale_because.append(artifact)

        return [a for a in affected if self._state[a].staleness is Staleness.STALE]

    def stale(self) -> list[ArtifactId]:
        return [
            a for a in _topological() if self._state[a].staleness is Staleness.STALE
        ]

    def buildable(self) -> list[ArtifactId]:
        """Stale or unbuilt artifacts whose upstreams are all fresh."""
        out: list[ArtifactId] = []
        for artifact in _topological():
            st = self._state[artifact]
            if st.staleness is Staleness.FRESH:
                continue
            upstreams = DEPENDS_ON.get(artifact, ())
            if all(self._state[u].staleness is Staleness.FRESH for u in upstreams):
                out.append(artifact)
        return out
