#!/usr/bin/env python3
"""Regenerate the baked feasibility corpus from the published Blueprint Library.

The inverse of the retired ``scripts/seed_feasibility_corpus.py`` (B→C): instead
of deriving the corpus from ``reference_data_products.yaml``, it enumerates every
PUBLISHED template, projects each to a ``FeasibilitySpec`` (ODCS→C via
``feasibility_map``), validates fail-closed, and rewrites
``reference/<domain>/<spec>.yaml`` + ``corpus.yaml`` (bumped ``corpus_version``)
+ ``index.yaml``. The (untouched) scanner keeps reading the baked corpus.

Also invoked in-process by ``POST /api/templates/{id}/publish``.

Usage:
    env/bin/python scripts/generate_feasibility_corpus_from_library.py [--version X.Y]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlmodel import Session  # noqa: E402

from workbench.backend import feasibility_spec as fs  # noqa: E402
from workbench.backend import template_corpus  # noqa: E402
from workbench.backend.database import engine  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Regenerate the feasibility corpus.")
    ap.add_argument("--version", default=None, help="corpus_version to stamp (else auto-bump)")
    args = ap.parse_args()

    with Session(engine) as session:
        result = template_corpus.regenerate_feasibility_corpus(session, version=args.version)
    print(json.dumps(result, indent=2, default=str))

    # Belt-and-suspenders: the freshly-written corpus must load fail-closed.
    specs = fs.load_corpus()
    print(f"Verified: {len(specs)} specs load under corpus_version {fs.corpus_version()}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
