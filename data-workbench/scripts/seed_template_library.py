#!/usr/bin/env python3
"""Bootstrap the Blueprint Library from the committed ODCS seed files.

Loads ``playbook/template_seed/**/*.yaml`` into the (empty) Library as read-only
``origin=seed``, ``status=published`` templates. Idempotent. Loading ODCS — no
lossy conversion happens here. Pass ``--with-corpus`` to also regenerate the
feasibility corpus afterwards (a fresh instance wants both).

Usage:
    env/bin/python scripts/seed_template_library.py [--with-corpus]
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

from workbench.backend import template_corpus  # noqa: E402
from workbench.backend.database import engine  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Seed the Blueprint Library.")
    ap.add_argument("--with-corpus", action="store_true",
                    help="also regenerate the feasibility corpus after seeding")
    args = ap.parse_args()

    with Session(engine) as session:
        result = template_corpus.seed_template_library(session)
        print(json.dumps({"seed": result}, indent=2, default=str))
        if result["errors"]:
            print(f"WARNING: {len(result['errors'])} seed file(s) failed to load.")
        if args.with_corpus:
            corpus = template_corpus.regenerate_feasibility_corpus(session)
            print(json.dumps({"corpus": corpus}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
