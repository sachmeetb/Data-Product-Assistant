#!/usr/bin/env python3
"""Backfill the normalized `searchName` property on graph identity nodes.

`searchName` (= `toLower(name)`) is written by discovery / odcs-to-dprod on new
nodes, but the discovery loader uses `ON CREATE SET`, so nodes loaded before
`searchName` shipped don't have it. This one-time, idempotent pass populates
`searchName = toLower(name)` on every `:Dataset` / `:Column` / `:DProdColumn`
that still lacks it, so the case-insensitive lookup resolution (write_mappings.py
/ lookup_via.py) resolves against a normalized key rather than falling back to an
inline `toLower(name)`. Safe to re-run.

Neo4j connection details are read from workbench.db (AppSettings), the same
source the backend uses.

Usage:
    env/bin/python scripts/backfill_search_name.py                 # all projects
    env/bin/python scripts/backfill_search_name.py --project-code dpe-sa-08132026-01
"""

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlmodel import Session  # noqa: E402

from workbench.backend.database import engine  # noqa: E402
from workbench.backend.models import AppSettings  # noqa: E402
from workbench.backend.neo4j_client import neo4j_session  # noqa: E402

# One statement per label. A project filter (URI CONTAINS the code) is applied
# only when --project-code is given; searchName is a pure per-node normalization
# with no cross-project concern, so an unscoped backfill is also safe.
_LABELS = ["Dataset", "Column", "DProdColumn"]


def _backfill_label(ns, label: str, project_code: str | None) -> int:
    where = "n.name IS NOT NULL AND n.searchName IS NULL"
    params: dict = {}
    if project_code:
        where += " AND n.uri CONTAINS $pc"
        params["pc"] = project_code
    q = (
        f"MATCH (n:{label}) WHERE {where} "
        f"SET n.searchName = toLower(n.name) RETURN count(n) AS c"
    )
    rec = ns.run(q, **params).single()
    return int(rec["c"]) if rec else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-code", default=None,
                        help="Restrict to nodes whose URI contains this code (default: all).")
    args = parser.parse_args()

    with Session(engine) as session:
        settings = session.get(AppSettings, 1) or AppSettings()

    scope = f"project '{args.project_code}'" if args.project_code else "ALL projects"
    print(f"Backfilling searchName on {', '.join(_LABELS)} for {scope}...")
    total = 0
    with neo4j_session(
        settings.neo4j_host, settings.neo4j_port,
        settings.neo4j_user, settings.neo4j_password, settings.neo4j_database,
    ) as ns:
        for label in _LABELS:
            n = _backfill_label(ns, label, args.project_code)
            total += n
            print(f"  :{label:14} updated {n}")
    print(f"Done. {total} node(s) received searchName.")


if __name__ == "__main__":
    main()
