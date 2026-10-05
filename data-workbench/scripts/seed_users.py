#!/usr/bin/env python3
"""Manage Data Workbench login accounts (the ``AppUser`` SQLite table).

Run from the repo root so the ``workbench.backend.*`` imports resolve:

    # create / update (upsert by email)
    env/bin/python scripts/seed_users.py \
        --email po@example.com --name "Pat Owner" --role owner --password 'secret'
    env/bin/python scripts/seed_users.py \
        --email eng@example.com --name "Erin Engineer" --role engineer --password 'secret'

    # list all accounts
    env/bin/python scripts/seed_users.py --list

    # hard-delete an account
    env/bin/python scripts/seed_users.py --delete --email po@example.com

    # deactivate (login refused) without deleting: re-upsert with --inactive
    env/bin/python scripts/seed_users.py --email po@example.com --role owner \
        --password 'secret' --inactive

Compose secrets (WB_AUTH_SECRET, WB_READ_ONLY, role-bound WB_MCP_TOKENS) belong
in the gitignored .env — see docs/auth.md.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the workbench backend package importable when run directly.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlmodel import Session, select

from workbench.backend.auth import ACCOUNT_ROLES, hash_password
from workbench.backend.database import create_db_and_tables, engine
from workbench.backend.models import AppUser


def _list_accounts() -> int:
    with Session(engine) as session:
        rows = session.exec(select(AppUser)).all()
    if not rows:
        print("(no accounts)")
        return 0
    width = max(len(r.email) for r in rows)
    print(f"{'EMAIL':<{width}}  {'ROLE':<9}  ACTIVE  NAME")
    for r in sorted(rows, key=lambda x: x.email):
        print(f"{r.email:<{width}}  {r.role:<9}  {'yes' if r.active else 'no ':<6}  {r.name}")
    print(f"\n{len(rows)} account(s).")
    return 0


def _delete_account(email: str) -> int:
    with Session(engine) as session:
        row = session.get(AppUser, email)
        if row is None:
            print(f"No account {email} — nothing to delete.", file=sys.stderr)
            return 1
        session.delete(row)
        session.commit()
    print(f"Deleted account {email}.")
    return 0


def _upsert_account(email: str, name: str, role: str, password: str, inactive: bool) -> int:
    if not role or not password:
        print("--role and --password are required to create/update an account.", file=sys.stderr)
        return 2
    with Session(engine) as session:
        row = session.get(AppUser, email)
        action = "Updated" if row else "Created"
        if row is None:
            row = AppUser(email=email)
        row.name = name or row.name or email.split("@")[0]
        row.role = role
        row.password_hash = hash_password(password)
        row.active = not inactive
        session.add(row)
        session.commit()
    print(f"{action} account {email} (role={role}, active={not inactive}).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage Data Workbench login accounts.")
    parser.add_argument("--email", help="Account email (required for upsert/delete).")
    parser.add_argument("--name", default="")
    parser.add_argument("--role", choices=sorted(ACCOUNT_ROLES),
                        help="Account role (required when creating/updating).")
    parser.add_argument("--password", help="Plaintext password (hashed on write).")
    parser.add_argument("--inactive", action="store_true",
                        help="Create/mark the account inactive (login refused).")
    parser.add_argument("--list", action="store_true", help="List all accounts and exit.")
    parser.add_argument("--delete", action="store_true",
                        help="Delete the --email account and exit.")
    args = parser.parse_args(argv)

    create_db_and_tables()

    if args.list:
        return _list_accounts()

    email = (args.email or "").strip().lower()
    if not email:
        print("--email is required (or use --list).", file=sys.stderr)
        return 2

    if args.delete:
        return _delete_account(email)

    return _upsert_account(email, args.name, args.role, args.password or "", args.inactive)


if __name__ == "__main__":
    raise SystemExit(main())
