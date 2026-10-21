#!/usr/bin/env python3
"""Backfill embeddings on :BusinessConcept nodes for Concept-Guided chat.

Concepts created before the embedding layer shipped carry no `embedding`
property and are invisible to vector search (the Concept-Guided retrieval
mode). This script embeds every active concept (optionally scoped to one
domain) using the local fastembed model and stores the vector + ensures the
Neo4j vector index exists. Idempotent — re-running overwrites the vectors.

Neo4j connection details are read from workbench.db (AppSettings), the same
source the backend uses.

Usage:
    env/bin/python scripts/backfill_concept_embeddings.py
    env/bin/python scripts/backfill_concept_embeddings.py --domain customer
"""

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlmodel import Session  # noqa: E402

from workbench.backend import business_concepts as concepts  # noqa: E402
from workbench.backend import embeddings  # noqa: E402
from workbench.backend.database import engine  # noqa: E402
from workbench.backend.models import AppSettings  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", default=None, help="Limit to one domain (default: all).")
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

    result = concepts.backfill_embeddings(settings, args.domain)
    scope = f"domain={args.domain!r}" if args.domain else "all domains"
    print(f"Backfill ({scope}): embedded={result['embedded']} skipped={result['skipped']}")


if __name__ == "__main__":
    main()
