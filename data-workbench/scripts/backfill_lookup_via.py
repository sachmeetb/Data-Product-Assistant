#!/usr/bin/env python3
"""Backfill :LOOKUP_VIA edges for a project's existing lookup mappings.

Lookup-kind :ColumnMapping nodes store their lookup table/columns only as bare
strings in transformParams, so products materialised before :LOOKUP_VIA shipped
have no graph edge to the lookup source — the marketplace / engineer lineage
can't show the upstream product the lookup actually reads from. This script
resolves those bare names and MERGEs the edges. Idempotent — safe to re-run.

Neo4j connection details are read from workbench.db (AppSettings, the same
source the backend uses), so you only need --project-code.

Usage:
    env/bin/python scripts/backfill_lookup_via.py --project-code dpe-cf-06032026-01
    env/bin/python scripts/backfill_lookup_via.py --project-code <code> --contract-id <id>
"""

import argparse
import sys
from pathlib import Path

# Make the workbench backend package importable when run from the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlmodel import Session  # noqa: E402

from workbench.backend.database import engine  # noqa: E402
from workbench.backend.models import AppSettings  # noqa: E402
from workbench.backend.neo4j_client import neo4j_session  # noqa: E402
from workbench.backend.lookup_via import reconcile_lookup_via  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-code", required=True, help="Project code to backfill.")
    parser.add_argument(
        "--contract-id",
        default=None,
        help="Consumer contract id (default: '<project-code>-contract').",
    )
    args = parser.parse_args()

    with Session(engine) as session:
        settings = session.get(AppSettings, 1) or AppSettings()

    print(f"Reconciling :LOOKUP_VIA edges for project '{args.project_code}'...")
    with neo4j_session(
        settings.neo4j_host, settings.neo4j_port,
        settings.neo4j_user, settings.neo4j_password, settings.neo4j_database,
    ) as ns:
        result = reconcile_lookup_via(ns, args.project_code, args.contract_id)

    print(
        f"Done.  mappings_resolved={result['resolved']}  "
        f"edges_created={result['edges']}  unresolved={len(result['unresolved'])}"
    )
    for u in result["unresolved"]:
        print(f"  UNRESOLVED  {u}", file=sys.stderr)


if __name__ == "__main__":
    main()
