"""Declarative flow graph.

Replaces the ~1400-line `if/elif` chains that drove both predecessors. Two
properties are non-negotiable, both learned from how they failed:

  1. Every edge carries an explicit guard. SILVER's stage 3 was unguarded, so
     after the flow reached its terminal step *any* subsequent message fell
     through and silently re-ran the entire design phase.
  2. When no guard passes, this raises. There is no implicit fallthrough.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class NodeKind(str, Enum):
    AGENT = "AGENT"                  # LLM-backed
    DETERMINISTIC = "DETERMINISTIC"  # pure Python, e.g. discovery
    GATE = "GATE"                    # human-in-the-loop
    TERMINAL = "TERMINAL"


Guard = Callable[[dict[str, Any]], bool]


def always(_: dict[str, Any]) -> bool:
    return True


@dataclass
class Node:
    name: str
    kind: NodeKind
    handler: str | None = None       # dotted ref, resolved by the runner
    produces: str | None = None      # artifact id this node writes
    consumes: list[str] = field(default_factory=list)


@dataclass
class Edge:
    src: str
    dst: str
    guard: Guard = always
    label: str = ""


class FlowError(RuntimeError):
    pass


class NoTransition(FlowError):
    """No guard passed. Deliberately fatal rather than a silent re-run."""


class FlowGraph:
    def __init__(self, nodes: list[Node], edges: list[Edge], entry: str) -> None:
        self._nodes = {n.name: n for n in nodes}
        self._edges = edges
        self.entry = entry
        if entry not in self._nodes:
            raise FlowError(f"entry node {entry!r} is not in the graph")

    def node(self, name: str) -> Node:
        try:
            return self._nodes[name]
        except KeyError:
            raise FlowError(f"unknown node {name!r}") from None

    def out_edges(self, name: str) -> list[Edge]:
        self.node(name)
        return [e for e in self._edges if e.src == name]

    def next(self, current: str, state: dict[str, Any]) -> Node:
        node = self.node(current)
        if node.kind is NodeKind.TERMINAL:
            raise NoTransition(
                f"{current!r} is terminal; nothing follows it. "
                "Start a new run rather than re-entering the flow."
            )

        for edge in self.out_edges(current):
            if edge.guard(state):
                return self.node(edge.dst)

        raise NoTransition(
            f"no guard passed leaving {current!r} "
            f"(candidates: {[e.dst for e in self.out_edges(current)]})"
        )

    def validate(self) -> list[str]:
        """Structural problems, as a list of human-readable complaints."""
        problems: list[str] = []

        for edge in self._edges:
            for end in (edge.src, edge.dst):
                if end not in self._nodes:
                    problems.append(f"edge {edge.src}->{edge.dst} references unknown node {end!r}")

        reachable = {self.entry}
        frontier = [self.entry]
        while frontier:
            current = frontier.pop()
            for edge in self._edges:
                if edge.src == current and edge.dst not in reachable:
                    reachable.add(edge.dst)
                    frontier.append(edge.dst)

        for name, node in self._nodes.items():
            if name not in reachable:
                problems.append(f"node {name!r} is unreachable from {self.entry!r}")
            if node.kind is not NodeKind.TERMINAL and not self.out_edges(name):
                problems.append(f"node {name!r} is non-terminal but has no outgoing edges")

        produced = {n.produces for n in self._nodes.values() if n.produces}
        for node in self._nodes.values():
            for needed in node.consumes:
                if needed not in produced:
                    problems.append(
                        f"node {node.name!r} consumes {needed!r}, which no node produces"
                    )

        return problems
