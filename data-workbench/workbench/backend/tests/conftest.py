"""Test harness for the acceptance-gate lifecycle + UI/MCP parity tests.

Critically: env vars are set BEFORE any `workbench.backend` import so the
SQLModel `engine` (created at import in `database.py` from `config.DATABASE_URL`)
binds to a throwaway SQLite file, and the MCP server boots in open mode
(`WB_MCP_ALLOW_INSECURE`) so `_serving_guard` doesn't refuse calls. No Neo4j is
required — the lifecycle handlers' graph writes are wrapped in try/except and
the SQLite side (ProductRequest/Project/StageRun) is what these tests assert on.
"""

from __future__ import annotations

import os
import tempfile

# --- env must be set before importing the backend ---------------------------
_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db", prefix="wb-test-")
os.close(_DB_FD)
os.environ["WB_DATABASE_URL"] = f"sqlite:///{_DB_PATH}"
os.environ["WB_MCP_ALLOW_INSECURE"] = "1"
os.environ.pop("WB_ENFORCE_ACCEPT_GATE", None)  # default warn-only unless a test sets it
# Auth is DISABLED by default so the existing suite is unaffected (dev engineer
# identity, no 401s). Auth tests opt in per-test via the `auth_env` fixture.
os.environ.pop("WB_AUTH_SECRET", None)
os.environ.pop("WB_AUTH_REQUIRE", None)
os.environ.pop("WB_READ_ONLY", None)

import pytest  # noqa: E402
from sqlmodel import Session, SQLModel  # noqa: E402

from workbench.backend.database import engine  # noqa: E402
from workbench.backend.models import (  # noqa: E402
    AppUser,
    IntakeEvent,
    IntakePendingDependency,
    IntakeSpawn,
    IntakeSubmission,
    MigrationPlanRow,
    Project,
    ProjectPhysicalSchema,
    ProductRequest,
    ProductRequestKind,
    ProductRequestStatus,
    SourceBinding,
    StageRun,
    Workflow,
)


@pytest.fixture(scope="session", autouse=True)
def _create_tables():
    SQLModel.metadata.create_all(engine)
    yield
    try:
        os.unlink(_DB_PATH)
    except OSError:
        pass


@pytest.fixture()
def session():
    with Session(engine) as s:
        yield s


@pytest.fixture(autouse=True)
def _clean_tables(_create_tables):
    """Each test starts from an empty ProductRequest/Project set."""
    from sqlmodel import select
    with Session(engine) as s:
        for model in (
            ProductRequest,
            StageRun,
            Workflow,
            SourceBinding,
            MigrationPlanRow,
            ProjectPhysicalSchema,
            Project,
            AppUser,
            IntakePendingDependency,
            IntakeSpawn,
            IntakeEvent,
            IntakeSubmission,
        ):
            for row in s.exec(select(model)).all():
                s.delete(row)
        s.commit()
    yield


@pytest.fixture()
def auth_env(monkeypatch):
    """Enable auth (WB_AUTH_SECRET) and seed one owner + one engineer account.

    Returns a dict of {role: (email, password)}. Env is reverted after the test
    by monkeypatch, so the rest of the suite stays auth-disabled.
    """
    from workbench.backend.auth import hash_password

    monkeypatch.setenv("WB_AUTH_SECRET", "test-secret-please-ignore")
    creds = {
        "owner": ("po@example.com", "owner-pass"),
        "engineer": ("eng@example.com", "eng-pass"),
    }
    with Session(engine) as s:
        for role, (email, pw) in creds.items():
            s.add(AppUser(email=email, name=email.split("@")[0], role=role,
                          password_hash=hash_password(pw), active=True))
        s.commit()
    return creds


_counter = {"n": 0}


@pytest.fixture()
def make_project_with_request():
    """Factory: create a Project + a ProductRequest at a chosen status.

    Returns (project, request). Project codes are unique per call.
    """
    def _make(
        archetype: str = "dpe-cf",
        status: ProductRequestStatus = ProductRequestStatus.submitted,
        kind: ProductRequestKind = ProductRequestKind.new,
    ):
        _counter["n"] += 1
        code = f"test-{archetype}-{_counter['n']:03d}"
        # expire_on_commit=False so the returned detached instances keep their
        # attribute values readable after the session closes.
        with Session(engine, expire_on_commit=False) as s:
            project = Project(
                project_code=code,
                name=f"Test {code}",
                pg_connection="",
                archetype=archetype,
                multi_workflow=True,
            )
            s.add(project)
            s.commit()
            s.refresh(project)
            request = ProductRequest(
                project_id=project.id,
                contract_id=f"{code}-contract",
                kind=kind,
                status=status,
                submitted_by="po@example.com",
            )
            s.add(request)
            s.commit()
            s.refresh(request)
            s.expunge_all()
            return project, request

    return _make
