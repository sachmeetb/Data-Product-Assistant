#!/usr/bin/env python3
"""Reconnect orphaned PO-authored DQ rules to their :DProdColumn.

The PO's wizard-authored quality rules persist as
:PropertyShape {ruleSource IN ['domain','user']} anchored to a :DProdColumn
via :ON_DPROD_COLUMN. Before the capture/reconnect fix in
_generate_dprod (routers/odcs.py), every run of the engineer's
``odcs_to_dprod`` stage deleted the :DProdColumn nodes (DPROD_WIPE's
DETACH DELETE) and rebuilt them — severing those edges and leaving the
PropertyShape nodes orphaned. Orphaned rules disappear from the
marketplace Quality tab, the domain_rules review queue, and the DQ-rule
summary counts (all of which traverse :ON_DPROD_COLUMN).

The going-forward fix prevents NEW orphaning, but rules that were already
orphaned (products whose odcs_to_dprod ran before the fix) have no edge
left to capture, so this one-shot backfill heals them.

Recovery key: the rule URI encodes the dataset physical name (token 3) and
the column name (token 4) — the exact ``ods.physicalName`` / ``pc.name``
values the rule was authored against:

    domain: rule:{project_code}:dprod:{dataset_physical}:{col_name}:{type}:{i}
    user:   rule:{project_code}:user:{dataset_physical}:{col_name}:{type}:{i}:{ts}

We match the rebuilt column by (ods.physicalName, pc.name) rather than
reconstructing the column URI, since pc.name need not equal the
property_physical_name the URI was built from. Idempotent — safe to re-run.

Neo4j connection details are read from workbench.db (AppSettings, the same
source the backend uses), so you only need --project-code.

Usage:
    env/bin/python scripts/backfill_dprod_rule_edges.py --project-code dpe-cf-06032026-01
    env/bin/python scripts/backfill_dprod_rule_edges.py --project-code <code> --contract-id <id>
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


# Orphaned domain/user rules for this project: scoped by the rule URI prefix
# (which embeds the project code), with no surviving :ON_DPROD_COLUMN edge.
_FIND_ORPHANED_RULES = """\
MATCH (ps:PropertyShape)
WHERE coalesce(ps.ruleSource, '') IN ['domain', 'user']
  AND ps.uri STARTS WITH 'rule:' + $project_code + ':'
  AND NOT EXISTS { MATCH (ps)-[:ON_DPROD_COLUMN]->(:DProdColumn) }
RETURN ps.uri AS uri, ps.ruleSource AS source
"""

# Reconnect a single orphaned rule to its rebuilt column, matched by the
# dataset physical name + column name parsed from the rule URI. Scoped to the
# contract so cross-project repair is impossible.
_RECONNECT_ONE = """\
MATCH (dp:DProdDataProduct {uri: 'dprod:' + $contract_id})
      -[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
      -[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
      -[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
WHERE ods.physicalName = $dataset AND pc.name = $col
WITH pc LIMIT 1
MATCH (ps:PropertyShape {uri: $rule_uri})
MERGE (ps)-[:ON_DPROD_COLUMN]->(pc)
RETURN pc.uri AS col_uri
"""


def _parse_rule_uri(uri: str):
    """Return (dataset_physical, col_name) from a domain/user rule URI, or None.

    Layout: rule:{project_code}:{dprod|user}:{dataset}:{col}:{type}:{i}[:{ts}]
    project_code never contains a ':' (hyphen-delimited), so token positions
    are stable. dataset / col are physical identifiers — no ':' expected.
    """
    parts = uri.split(":")
    if len(parts) < 7 or parts[0] != "rule" or parts[2] not in ("dprod", "user"):
        return None
    return parts[3], parts[4]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-code", required=True, help="Project code to backfill.")
    parser.add_argument(
        "--contract-id",
        default=None,
        help="Consumer contract id (default: '<project-code>-contract').",
    )
    args = parser.parse_args()
    contract_id = args.contract_id or f"{args.project_code}-contract"

    with Session(engine) as session:
        settings = session.get(AppSettings, 1) or AppSettings()

    print(f"Reconnecting orphaned DQ rules for project '{args.project_code}'...")
    reconnected = 0
    unresolved: list[str] = []
    with neo4j_session(
        settings.neo4j_host, settings.neo4j_port,
        settings.neo4j_user, settings.neo4j_password, settings.neo4j_database,
    ) as ns:
        orphans = [dict(r) for r in ns.run(_FIND_ORPHANED_RULES, project_code=args.project_code)]
        for o in orphans:
            parsed = _parse_rule_uri(o["uri"])
            if not parsed:
                unresolved.append(f"{o['uri']} (unparseable URI)")
                continue
            dataset, col = parsed
            row = ns.run(
                _RECONNECT_ONE,
                contract_id=contract_id,
                dataset=dataset,
                col=col,
                rule_uri=o["uri"],
            ).single()
            if row and row.get("col_uri"):
                reconnected += 1
            else:
                unresolved.append(f"{o['uri']} (no column '{col}' in dataset '{dataset}')")

    print(f"Done.  orphans_found={len(orphans)}  reconnected={reconnected}  unresolved={len(unresolved)}")
    for u in unresolved:
        print(f"  UNRESOLVED  {u}", file=sys.stderr)


if __name__ == "__main__":
    main()
