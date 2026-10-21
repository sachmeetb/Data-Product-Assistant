#!/usr/bin/env python3
"""Backfill the PO owner on intake-scaffolded projects that predate the fix.

Intake/assembly-scaffolded `dpe-sa` (and `dpe-cf`) projects were created with
`Project.owner_email = None`, so the Data Product Owner couldn't find them in
My Products (web in-flight + PO-MCP both key on the owner). The scaffold now
threads the PO's email (`IntakeSubmission.reviewed_by`) into `owner_email` /
`owner_name`; this one-time, idempotent pass repairs the ALREADY-scaffolded
projects so they don't have to be re-created from scratch.

For every `IntakeSpawn` with a `child_project_id`, it resolves the child project
and its originating submission and, when `Project.owner_email` is empty and the
submission's `reviewed_by` is email-shaped, sets `owner_email` / `owner_name`
(via the same `intake_scaffold._owner_of` logic the scaffold uses). A machine
principal (external-tool intake with no reviewer) is skipped — never invented.

Connection: this touches ONLY the SQLite workbench.db (no Neo4j).

Usage:
    env/bin/python scripts/backfill_intake_owner_email.py            # apply
    env/bin/python scripts/backfill_intake_owner_email.py --dry-run  # report only
    # in the compose stack:
    docker compose exec -T backend python scripts/backfill_intake_owner_email.py
"""

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlmodel import Session, select  # noqa: E402

from workbench.backend.database import engine  # noqa: E402
from workbench.backend.intake_scaffold import _owner_of  # noqa: E402
from workbench.backend.models import IntakeSpawn, IntakeSubmission, Project  # noqa: E402


def backfill(session: Session, *, dry_run: bool = False) -> dict:
    """Repair owner_email on intake-scaffolded projects. Returns a small report."""
    updated, skipped_has_owner, skipped_no_email, missing = 0, 0, 0, 0
    spawns = session.exec(
        select(IntakeSpawn).where(IntakeSpawn.child_project_id.is_not(None))  # type: ignore[union-attr]
    ).all()
    for spawn in spawns:
        project = session.get(Project, spawn.child_project_id)
        submission = session.get(IntakeSubmission, spawn.intake_submission_id)
        if project is None or submission is None:
            missing += 1
            continue
        if (project.owner_email or "").strip():
            skipped_has_owner += 1
            continue
        owner_email, owner_name = _owner_of(session, submission)
        if not owner_email:
            skipped_no_email += 1  # machine principal / no reviewer — never invent
            continue
        print(
            f"  {project.project_code} ({project.archetype}): owner_email -> {owner_email}"
            + (" [dry-run]" if dry_run else "")
        )
        if not dry_run:
            project.owner_email = owner_email
            if owner_name and not (project.owner_name or "").strip():
                project.owner_name = owner_name
            session.add(project)
        updated += 1
    if not dry_run:
        session.commit()
    return {
        "updated": updated,
        "skipped_has_owner": skipped_has_owner,
        "skipped_no_email": skipped_no_email,
        "missing": missing,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would change without writing.")
    args = parser.parse_args()

    print("Backfilling owner_email on intake-scaffolded projects"
          + (" (dry-run)" if args.dry_run else "") + "...")
    with Session(engine) as session:
        report = backfill(session, dry_run=args.dry_run)
    print(
        f"Done. updated={report['updated']} "
        f"skipped_has_owner={report['skipped_has_owner']} "
        f"skipped_no_email={report['skipped_no_email']} "
        f"missing={report['missing']}"
    )


if __name__ == "__main__":
    main()
