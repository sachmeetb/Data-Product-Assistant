#!/usr/bin/env python3
"""Audit existing active :CONSUMES edges for pre-existing self / cycle violations.

Run this ONCE before enabling the Phase 0 DAG guard (see `_contract_versioning.
validate_consumes_bindings`). Historically nothing recursed over :CONSUMES, so a
self-loop or A→B→A cycle could have been created silently. The new guard rejects
such bindings at save time — but a legacy graph that already contains one would
wedge the next save of the offending consumer. This detect-only pass surfaces
them so they can be untangled first.

It reuses the SAME pure BFS + active-edge neighbor query the runtime guard uses,
so what it flags is exactly what the guard would reject. Read-only; never mutates.

Neo4j connection details are read from workbench.db (AppSettings), the same
source the backend uses.

Usage:
    env/bin/python scripts/audit_consumes_cycles.py                 # all projects
    env/bin/python scripts/audit_consumes_cycles.py --project-code dpe-cf-08132026-01
"""

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlmodel import Session  # noqa: E402

from workbench.backend._contract_versioning import (  # noqa: E402
    CONSUMABLE_LIFECYCLE_STATES,
    CONSUMES_UPSTREAM_NEIGHBORS,
    would_create_cycle,
)
from workbench.backend.database import engine  # noqa: E402
from workbench.backend.models import AppSettings  # noqa: E402
from workbench.backend.neo4j_client import neo4j_session  # noqa: E402

# Every consumer contract + the product URIs it currently :CONSUMES (active
# edges only). We re-derive each edge as a "proposed binding" and ask whether it
# would (already) close a cycle given the OTHER active edges — i.e. is this graph
# already non-DAG?
_ALL_ACTIVE_CONSUMES = """\
MATCH (dc:DataContract)-[r:CONSUMES]->(dp:DProdDataProduct)
WHERE r.toVersion IS NULL
RETURN dc.id AS consumer_contract_id, dp.uri AS dprod_uri
ORDER BY dc.id, dp.uri
"""


def _target_contract_id(dprod_uri: str) -> str:
    prefix = "dprod:"
    return dprod_uri[len(prefix):] if dprod_uri.startswith(prefix) else ""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-code", default=None,
                        help="Restrict to a single consumer contract's edges "
                             "(matches the '{code}-contract' id prefix).")
    args = parser.parse_args()

    try:
        with Session(engine) as session:
            settings = session.get(AppSettings, 1)
    except Exception as e:  # e.g. an un-initialised / empty workbench.db
        print(
            "Could not read AppSettings from the Workbench DB "
            f"({engine.url}): {e}\n"
            "Run this in the environment where the real workbench.db is "
            "initialised (set WB_DATABASE_URL if it lives elsewhere).",
            file=sys.stderr,
        )
        return 2
    if settings is None or not settings.neo4j_host:
        print(
            "No Neo4j connection is configured in AppSettings — nothing to audit. "
            "Configure the graph connection first (Settings → Neo4j), then re-run.",
            file=sys.stderr,
        )
        return 2

    scope = f"project '{args.project_code}'" if args.project_code else "ALL projects"
    print(f"Auditing active :CONSUMES edges for self/cycle violations ({scope})...")

    violations: list[tuple[str, str, str]] = []  # (consumer, dprod_uri, reason)
    # neo4j_session connects LAZILY (on first query), so an unreachable graph
    # surfaces inside the block — wrap the whole run, not just the open.
    try:
        with neo4j_session(
            settings.neo4j_host, settings.neo4j_port,
            settings.neo4j_user, settings.neo4j_password, settings.neo4j_database,
        ) as ns:
            def neighbors(cid: str) -> set[str]:
                return {
                    r["upstream_contract_id"]
                    for r in ns.run(
                        CONSUMES_UPSTREAM_NEIGHBORS,
                        contract_id=cid,
                        consumable_states=list(CONSUMABLE_LIFECYCLE_STATES),
                    )
                    if r["upstream_contract_id"]
                }

            edges = list(ns.run(_ALL_ACTIVE_CONSUMES))
            for row in edges:
                consumer = row["consumer_contract_id"]
                dprod_uri = row["dprod_uri"]
                if args.project_code and not consumer.startswith(f"{args.project_code}-contract"):
                    continue
                self_uri = f"dprod:{consumer}"
                if dprod_uri == self_uri:
                    violations.append((consumer, dprod_uri, "self-loop"))
                    continue
                target = _target_contract_id(dprod_uri)
                # Ask: does `target` already reach `consumer` via the OTHER active
                # edges? (The neighbor query includes this edge, but BFS starts at
                # target and only cares about a return path to consumer.)
                if would_create_cycle(neighbors, consumer, target):
                    violations.append((consumer, dprod_uri, "cycle"))

            print(f"  scanned {len(edges)} active :CONSUMES edge(s).")
    except Exception as e:
        print(f"Could not query Neo4j at {settings.neo4j_host}:{settings.neo4j_port}: {e}",
              file=sys.stderr)
        return 2

    if not violations:
        print("Clean — no self/cycle violations. Safe to enable the guard.")
        return 0

    print(f"\nFOUND {len(violations)} violation(s):")
    for consumer, uri, reason in violations:
        print(f"  [{reason}] {consumer}  --CONSUMES-->  {uri}")
    print("\nUntangle these (re-version the offending consumer without the "
          "offending edge) before relying on the guard.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
