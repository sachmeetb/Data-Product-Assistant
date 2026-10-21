#!/usr/bin/env python3
"""Backfill text-similarity vectors on :EstateColumn nodes for feasibility reuse.

Connected-Estate columns get a stored `embedding` (+ `embeddingTextHash` /
`embeddingModel`) written at enrichment time; feasibility then READS the vector
instead of recomputing it every run. Scans enriched before graph-native vectors
shipped (or after a model swap) carry no current vector — this one-time,
idempotent pass embeds every non-deleted column of a scan whose stored text-hash
+ model don't match the current text + model, and ensures the native vector index
(`estate_column_embedding`) exists. Content-hash keyed, so re-runs only touch what
changed.

Neo4j connection details are read from workbench.db (AppSettings), the same
source the backend uses.

Usage:
    env/bin/python scripts/backfill_estate_embeddings.py --scan-id 42
    env/bin/python scripts/backfill_estate_embeddings.py --estate-id 3   # all its scans
"""

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlmodel import Session, select  # noqa: E402

from workbench.backend import embeddings  # noqa: E402
from workbench.backend import estate as estate_mod  # noqa: E402
from workbench.backend.database import engine  # noqa: E402
from workbench.backend.models import AppSettings, EstateScan  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--scan-id", type=int, help="Backfill a single scan.")
    g.add_argument("--estate-id", type=int, help="Backfill every scan of an estate.")
    args = parser.parse_args()

    if not embeddings.available():
        print(
            "Embedding model unavailable (fastembed not installed or model "
            "load failed). Nothing embedded.",
            file=sys.stderr,
        )
        sys.exit(1)

    with Session(engine) as session:
        settings = session.get(AppSettings, 1) or AppSettings()
        if args.scan_id is not None:
            scan_ids = [args.scan_id]
        else:
            scan_ids = [s.id for s in session.exec(
                select(EstateScan).where(EstateScan.estate_id == args.estate_id)
            ).all()]

    if not scan_ids:
        print("No scans matched.", file=sys.stderr)
        sys.exit(1)

    for sid in scan_ids:
        with Session(engine) as session:
            settings = session.get(AppSettings, 1) or AppSettings()
        result = estate_mod.backfill_estate_embeddings(settings, sid)
        print(f"scan {sid}: embedded={result['embedded']} "
              f"skipped={result['skipped']} candidates={result['candidates']}")


if __name__ == "__main__":
    main()
