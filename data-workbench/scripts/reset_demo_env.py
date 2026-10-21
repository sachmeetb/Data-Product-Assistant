"""Clean-slate reset for demo / end-to-end validation.

Gives a truly fresh state so stale artifacts can't skew a test run (per Niel's
"start from a clean environment" guidance):
  • Neo4j  — every node/relationship deleted (fresh graph).
  • SQLite — all project/workflow/run/request/chat rows cleared (AppSettings +
             LlmUsageEvent kept: workbench config + the usage ledger).
  • Source Postgres — every deployed `vw_*` VIEW dropped; BASE TABLES preserved
             (players/teams/… — the input data stays intact).

Run inside the backend container (PYTHONPATH=/app so `workbench` imports):
    docker exec -e PYTHONPATH=/app data-workbench-backend-1 \
        python scripts/reset_demo_env.py --yes

Without --yes it prints what it WOULD do and exits (dry run).
"""
from __future__ import annotations

import os
import sys

from sqlmodel import Session, select, delete

from workbench.backend.database import engine
from workbench.backend.models import (
    Project, Workflow, StageRun, StageExecution, DQTestRun, ProductRequest,
    MarketplaceGap, IngestDraft, ProductChatSession, ProductChatMessage,
    ChatSession, ChatMessage, SemanticChatSession, SemanticChatMessage,
    MaterializationTarget, MigrationPlanRow, SourceBinding, ExecutionProfile,
    PlatformConnection,
)

# Order matters: children before parents (FKs). AppSettings + LlmUsageEvent kept.
# Registered platform connections + their bindings/profiles ARE wiped — a
# clean-slate reset should leave no operator-registered sources/targets behind.
_SQLITE_WIPE_ORDER = [
    ChatMessage, ChatSession, ProductChatMessage, ProductChatSession,
    SemanticChatMessage, SemanticChatSession, StageExecution, StageRun,
    DQTestRun, MarketplaceGap, MaterializationTarget, IngestDraft,
    MigrationPlanRow, ProductRequest, Workflow,
    SourceBinding, ExecutionProfile, Project, PlatformConnection,
]


def _source_pg_dsns(session) -> list[str]:
    """EVERY project's source connection (before we wipe) so we drop `vw_*` on
    all of them, not just the first — a demo with two source projects on
    different sample DBs would otherwise leave one project's views behind.
    Falls back to the known local lakers DSN when no project has a connection."""
    projs = session.exec(select(Project).where(Project.pg_connection != None)).all()  # noqa: E711
    dsns: list[str] = []
    for p in projs:
        if p.pg_connection and p.pg_connection not in dsns:
            dsns.append(p.pg_connection)
    if dsns:
        return dsns
    return [os.environ.get(
        "WB_DEMO_SOURCE_DSN",
        "postgresql://lakers:lakers123@host.docker.internal:5432/lakers",
    )]


def drop_deployed_views(dsn: str, apply: bool) -> list[str]:
    import psycopg2
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(
        "SELECT table_schema, table_name FROM information_schema.views "
        "WHERE table_name LIKE 'vw_%' ORDER BY table_name"
    )
    views = [(r[0], r[1]) for r in cur.fetchall()]
    dropped = []
    if apply:
        for schema, name in views:
            cur.execute(f'DROP VIEW IF EXISTS "{schema}"."{name}" CASCADE')
            dropped.append(f"{schema}.{name}")
    conn.close()
    return dropped if apply else [f"{s}.{n}" for s, n in views]


def wipe_neo4j(apply: bool) -> int:
    from neo4j import GraphDatabase
    host = os.environ.get("WB_NEO4J_HOST", "neo4j")
    port = os.environ.get("WB_NEO4J_PORT", "7687")
    user = os.environ.get("WB_NEO4J_USER", "neo4j")
    pw = os.environ.get("WB_NEO4J_PASSWORD", "workbenchpass")
    drv = GraphDatabase.driver(f"bolt://{host}:{port}", auth=(user, pw))
    with drv.session() as gs:
        n = gs.run("MATCH (n) RETURN count(n) AS n").single()["n"]
        if apply:
            gs.run("MATCH (n) DETACH DELETE n")
    drv.close()
    return n


def wipe_sqlite(apply: bool) -> dict:
    counts = {}
    with Session(engine) as s:
        for model in _SQLITE_WIPE_ORDER:
            counts[model.__name__] = len(s.exec(select(model)).all())
            if apply:
                s.exec(delete(model))
        if apply:
            s.commit()
    return counts


def _looks_like_demo(dsn: str) -> bool:
    """The throwaway demo target — the only thing this script may wipe. Both the
    hard-coded fallback DSN and e2e_uxrun.py use host.docker.internal + the
    `lakers` demo DB, so either signal marks the disposable demo. The `dwb` CLI
    additionally sets WB_DEMO_RESET_OK=1 because it KNOWS these are its own
    throwaway sample-DB containers (which carry neither signal in their DSN)."""
    if os.environ.get("WB_DEMO_RESET_OK") == "1":
        return True
    d = (dsn or "").lower()
    return "host.docker.internal" in d or "/lakers" in d


def _guard(apply: bool, dsn: str) -> None:
    """Refuse to apply destructive changes unless explicitly opted in AND the
    target looks like the throwaway demo. This script does an UNSCOPED Neo4j
    DETACH DELETE + drops all vw_* views — it must never run against a shared or
    production environment. Mirrors the WB_MCP_ALLOW_INSECURE fail-closed idiom."""
    if not apply:
        return
    if os.environ.get("WB_ALLOW_DEMO_RESET") != "1":
        print(
            "REFUSING to apply: set WB_ALLOW_DEMO_RESET=1 to confirm you really "
            "want to wipe the demo graph, projects, and vw_* views. (Drop --yes "
            "for a dry run.)"
        )
        sys.exit(1)
    if not _looks_like_demo(dsn):
        print(
            f"REFUSING to apply: target DSN does not look like the throwaway demo "
            f"(expected host.docker.internal or the 'lakers' DB, got: {dsn!r}). "
            f"This script must never run against a shared/production database."
        )
        sys.exit(1)


def main() -> None:
    apply = "--yes" in sys.argv
    with Session(engine) as s:
        dsns = _source_pg_dsns(s)

    # Guard on the primary DSN (WB_DEMO_RESET_OK, when set by the CLI, clears
    # every DSN); refuse if ANY target fails the demo check to stay fail-closed.
    for dsn in dsns:
        _guard(apply, dsn)

    print("=== Clean-slate reset " + ("(APPLYING)" if apply else "(DRY RUN — pass --yes to apply)") + " ===")
    all_views: list[str] = []
    for dsn in dsns:
        try:
            views = drop_deployed_views(dsn, apply)
        except Exception as e:  # noqa: BLE001 — a dead sample DB shouldn't abort the graph/SQLite wipe
            print(f"  (skipped views on {dsn!r}: {e})")
            continue
        all_views += views
    views = all_views
    print(f"  Postgres views {'dropped' if apply else 'that WOULD be dropped'} across "
          f"{len(dsns)} DSN(s) ({len(views)}): {views or '—'}")
    graph_n = wipe_neo4j(apply)
    print(f"  Neo4j nodes {'deleted' if apply else 'present'}: {graph_n}")
    counts = wipe_sqlite(apply)
    total = sum(counts.values())
    print(f"  SQLite rows {'cleared' if apply else 'present'} ({total}): " +
          ", ".join(f"{k}={v}" for k, v in counts.items() if v))
    print("=== done ===" if apply else "=== dry run only — re-run with --yes ===")
    print("Preserved: AppSettings, LlmUsageEvent, and all SOURCE tables (players/teams/…).")


if __name__ == "__main__":
    main()
