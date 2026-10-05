from sqlmodel import SQLModel, Session, create_engine

from .config import DATABASE_URL

engine = create_engine(DATABASE_URL, echo=False)


def create_db_and_tables():
    SQLModel.metadata.create_all(engine)
    _migrate(engine)


def _migrate(eng):
    """Add columns that SQLite create_all won't add to existing tables."""
    migrations = [
        "ALTER TABLE project ADD COLUMN archetype TEXT DEFAULT 'dpe'",
        "ALTER TABLE project ADD COLUMN workflow_json TEXT",
        "ALTER TABLE project ADD COLUMN domain TEXT",
        "ALTER TABLE project ADD COLUMN published_at TIMESTAMP",
        "ALTER TABLE project ADD COLUMN multi_workflow BOOLEAN DEFAULT 0",
        # Archetype slug renames
        "UPDATE project SET archetype = 'dd' WHERE archetype = 'dpe'",
        "UPDATE project SET archetype = 'dpe-cf' WHERE archetype = 'dpe-full'",
        # Multi-workflow: add workflow_id to stage runs
        "ALTER TABLE stagerun ADD COLUMN workflow_id TEXT",
        # dpe-sa: surface "ready to validate" state on PO dashboard.
        "ALTER TABLE project ADD COLUMN discovery_complete_at TIMESTAMP",
        # dpe-sa: PO's free-form idea text from NewSourceProductWizard.
        "ALTER TABLE project ADD COLUMN product_idea TEXT",
        # PO email — flows into synthesized ODCS owners[] for SA products,
        # which gets the product to surface in the PO's My Products list.
        "ALTER TABLE project ADD COLUMN owner_email TEXT",
        # PO display name — used by sa_pipeline to populate
        # :DataContractOwner.name so the marketplace card shows the actual
        # name instead of an email prefix.
        "ALTER TABLE project ADD COLUMN owner_name TEXT",
        # Consumer-aligned ingest: link source ProductRequests back to the
        # parent IngestDraft that spawned them via "Create now" gap resolution.
        "ALTER TABLE productrequest ADD COLUMN parent_ingest_draft_id INTEGER",
        # Engineer→PO `source_candidates_needed` requests carry optional gap
        # context (specific column the engineer couldn't map, free-text reason).
        "ALTER TABLE productrequest ADD COLUMN gap_column_uri TEXT",
        "ALTER TABLE productrequest ADD COLUMN gap_reason TEXT",
        # Phase 4: non-Postgres materialization targets store platform config here.
        # Credentials are NEVER stored — secret_ref inside the JSON is an env var name.
        "ALTER TABLE materializationtarget ADD COLUMN connection_json TEXT DEFAULT '{}'",
        # Connection roles: ["source"] and/or ["target"] — labels the operator's
        # intended use for this connection (discovery/view-source vs dbt target).
        "ALTER TABLE platformconnection ADD COLUMN connection_roles_json TEXT DEFAULT '[\"source\"]'",
        # Target SQL dialect override on SourceBinding — lets an engineer specify a
        # different dialect than the source platform (e.g. source=MySQL, target=postgres).
        # Set by ConnectionPickerDialog or the configure_serving stage dialog.
        "ALTER TABLE sourcebinding ADD COLUMN target_dialect TEXT DEFAULT ''",
        "ALTER TABLE sourcebinding ADD COLUMN view_target_namespace TEXT DEFAULT ''",
        # Product-level serving target: points at a registered PlatformConnection
        # (role "target") used as the serving target for ALL modes. Unset → borrow
        # the source connection (today's same-instance behaviour).
        "ALTER TABLE materializationtarget ADD COLUMN target_connection_id INTEGER",
        "ALTER TABLE materializationtarget ADD COLUMN view_target_namespace TEXT DEFAULT ''",
        # Git integration settings (push serving artifacts to a repo per product).
        "ALTER TABLE appsettings ADD COLUMN git_provider TEXT",
        "ALTER TABLE appsettings ADD COLUMN git_base_url TEXT",
        "ALTER TABLE appsettings ADD COLUMN git_web_base_url TEXT",
        "ALTER TABLE appsettings ADD COLUMN git_token TEXT",
        "ALTER TABLE appsettings ADD COLUMN git_org TEXT",
        "ALTER TABLE appsettings ADD COLUMN git_auto_push INTEGER DEFAULT 0",
        # dmig: target catalog (writable catalog half of catalog.schema) for
        # 3-level migration targets (Databricks/Snowflake), chosen in Configure
        # Migration. Added after the migrationplanrow table already existed.
        "ALTER TABLE migrationplanrow ADD COLUMN target_catalog TEXT DEFAULT ''",
        # Inbound intake: link projects/requests scaffolded by the intake saga
        # back to the originating IntakeSubmission (distinct from the ingest-draft
        # link). New intake* tables are created fresh by create_all — no ALTER.
        "ALTER TABLE productrequest ADD COLUMN parent_intake_submission_id INTEGER",
        "ALTER TABLE project ADD COLUMN parent_intake_submission_id INTEGER",
        "ALTER TABLE project ADD COLUMN data_connectivity_mode TEXT DEFAULT 'live'",
        "ALTER TABLE intakesubmission ADD COLUMN execution_mode TEXT DEFAULT 'live'",
        # Connected Estate: an EstateSource is scoped to one Unity Catalog /
        # database container (catalog-per-source model). Empty for 2-level
        # platforms. Drives the live connect (extra_config.catalog) + the {db}
        # segment of the estate dataset URIs.
        "ALTER TABLE estatesource ADD COLUMN catalog TEXT DEFAULT ''",
        # Feasibility: a run now spans the latest scan of EVERY enabled source in
        # the estate (multi-catalog assessment), not a single pinned scan. The
        # scan_id column is kept (newest contributing scan) for back-compat.
        "ALTER TABLE feasibilityrun ADD COLUMN scan_ids_json TEXT DEFAULT '[]'",
        # Feasibility: optional spec_ids filter (UI spec picker); empty = no filter.
        "ALTER TABLE feasibilityrun ADD COLUMN spec_ids_json TEXT DEFAULT '[]'",
        # Feasibility: tiered schema-scoping levers {enabled, floor, cap}; {} = defaults.
        "ALTER TABLE feasibilityrun ADD COLUMN schema_scoping_json TEXT DEFAULT '{}'",
        # Feasibility R2: live evaluation progress {stage, specs_total, specs_done, …}.
        "ALTER TABLE feasibilityrun ADD COLUMN progress_json TEXT DEFAULT '{}'",
        # Estate metadata enrichment state: None | enriching | enriched | failed.
        # Added to existing EstateScan rows via ALTER (new rows get it from create_all).
        "ALTER TABLE estatescan ADD COLUMN enrichment_state TEXT",
        "ALTER TABLE estatescan ADD COLUMN enriched_at TIMESTAMP",
        "ALTER TABLE estatescan ADD COLUMN enrichment_progress_json TEXT",
        "ALTER TABLE estatescan ADD COLUMN enrichment_summary_json TEXT",
        # R3 WS1: live metadata-scan progress {namespaces_total, namespaces_done, …}.
        "ALTER TABLE estatescan ADD COLUMN scan_progress_json TEXT DEFAULT '{}'",
        # Product Assembly: a `structured` submission (native ODCS, no LLM parse) vs
        # the classic `parsed` inbound path. Drives the light-confirm review.
        "ALTER TABLE intakesubmission ADD COLUMN ingestion_mode TEXT DEFAULT 'parsed'",
        # Product Assembly: the deterministic functional report, cached on the assembly.
        "ALTER TABLE productassembly ADD COLUMN report_md TEXT",
        "ALTER TABLE productassembly ADD COLUMN report_generated_at TIMESTAMP",
        # Connected-Estate scaffold: the pre-selected Data Discovery table scope
        # carried onto a dpe-sa project (a JSON list of "schema.table").
        "ALTER TABLE project ADD COLUMN discovery_scope_json TEXT",
        # Offline extraction: an EstateSource is either `live` (DW connects + scans)
        # or `offline` (the client runs the extraction kit + uploads a manifest DW
        # replays). Offline sources carry no connection.
        "ALTER TABLE estatesource ADD COLUMN ingest_mode TEXT DEFAULT 'live'",
    ]
    with eng.connect() as conn:
        for sql in migrations:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception:
                conn.rollback()

        # Constraint change: make estatesource.connection_id NULLABLE so offline
        # sources (no PlatformConnection) can be added. SQLite can't ALTER a NOT NULL
        # away, so rebuild the table from its own stored CREATE SQL (preserves every
        # column incl. later-ALTERed ones) with the one constraint dropped, then
        # replay its indexes. Idempotent — fires ONLY while the old NOT NULL is present.
        try:
            _make_estatesource_connection_nullable(conn)
        except Exception:
            conn.rollback()

        # Zombie sweep: project-scoped rows whose started_at predates the
        # linked project's created_at belong to a previously-deleted project
        # whose integer primary key has since been recycled by SQLite. Safe
        # to re-run on every startup — self-filtering by the timestamp join.
        zombie_cleanups = [
            """
            DELETE FROM stageexecution
             WHERE id IN (
               SELECT se.id FROM stageexecution se
               JOIN project p ON p.id = se.project_id
               WHERE se.started_at < p.created_at
             )
            """,
            """
            DELETE FROM dqtestrun
             WHERE id IN (
               SELECT d.id FROM dqtestrun d
               JOIN project p ON p.id = d.project_id
               WHERE d.started_at < p.created_at
             )
            """,
        ]
        for sql in zombie_cleanups:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception:
                conn.rollback()


def _make_estatesource_connection_nullable(conn) -> None:
    """Rebuild ``estatesource`` with a NULLABLE ``connection_id`` (SQLite only).

    Guarded + idempotent: reads the live CREATE TABLE SQL and no-ops unless it still
    carries ``connection_id INTEGER NOT NULL``. Rebuilds via a temp table derived
    from that same SQL (so every existing column, including ALTER-added ones, is
    preserved in order — ``INSERT ... SELECT *`` matches), then replays the original
    indexes. FK enforcement is off in this DB, so orphan/NULL connection_ids are fine.
    """
    row = conn.execute(text(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='estatesource'"
    )).fetchone()
    if not row or not row[0]:
        return
    create_sql = row[0]
    if "connection_id INTEGER NOT NULL" not in create_sql:
        return  # already nullable — nothing to do
    idx_rows = conn.execute(text(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='estatesource' "
        "AND sql IS NOT NULL"
    )).fetchall()
    new_sql = create_sql.replace(
        "connection_id INTEGER NOT NULL", "connection_id INTEGER"
    ).replace("CREATE TABLE estatesource", "CREATE TABLE estatesource_new", 1)
    conn.execute(text(new_sql))
    conn.execute(text("INSERT INTO estatesource_new SELECT * FROM estatesource"))
    conn.execute(text("DROP TABLE estatesource"))
    conn.execute(text("ALTER TABLE estatesource_new RENAME TO estatesource"))
    for (idx_sql,) in idx_rows:
        try:
            conn.execute(text(idx_sql))
        except Exception:
            pass
    conn.commit()


from sqlalchemy import text


def get_session():
    with Session(engine) as session:
        yield session
