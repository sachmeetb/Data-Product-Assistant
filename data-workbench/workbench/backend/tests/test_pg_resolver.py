"""Phase 2 — stable connection identity for the execution-location planner.

The recursive :CONSUMES borrow + virtual-view served-map need a live Neo4j +
Postgres to exercise end-to-end (the graph query shapes and multi-hop chain), so
those are covered by the opt-in live module + the manual E2E in the plan. Here we
unit-test the pure identity helper that decides whether two required relations
live on the SAME physical instance (a single view can't span two instances even
of the same engine).
"""

from workbench.backend.pg_resolver import connection_identity


def test_structured_ref_identity_is_stable():
    # connection_identity reads ONLY the structured ref (host/port/db) — there is
    # no legacy pg_connection DSN-parse arm to reconcile against.
    a = connection_identity(
        "postgres", {"host": "db.example.com", "port": 5432, "database": "analytics"})
    b = connection_identity(
        "postgres", {"host": "db.example.com", "port": 5432, "database": "analytics"})
    assert a == b
    # A retired {"pg_connection": ...} shape carries no host → resolves as empty
    # identity, NOT the structured instance's.
    assert connection_identity("postgres", {"pg_connection": "postgresql://u:p@db.example.com:5432/analytics"}) != a


def test_postgresql_alias_normalises_to_postgres():
    a = connection_identity("postgresql", {"host": "h", "port": 5432, "database": "d"})
    b = connection_identity("postgres", {"host": "h", "port": 5432, "database": "d"})
    assert a == b


def test_different_instance_same_engine_differs():
    a = connection_identity("postgres", {"host": "host-a", "port": 5432, "database": "d"})
    b = connection_identity("postgres", {"host": "host-b", "port": 5432, "database": "d"})
    assert a != b


def test_different_database_same_host_differs():
    a = connection_identity("postgres", {"host": "h", "port": 5432, "database": "db1"})
    b = connection_identity("postgres", {"host": "h", "port": 5432, "database": "db2"})
    assert a != b


def test_snowflake_account_disambiguates():
    a = connection_identity("snowflake", {"host": "", "database": "DW", "extra_config": {"account": "acct1"}})
    b = connection_identity("snowflake", {"host": "", "database": "DW", "extra_config": {"account": "acct2"}})
    assert a != b


def test_databricks_http_path_disambiguates():
    a = connection_identity("databricks", {"host": "h", "extra_config": {"http_path": "/w/1"}})
    b = connection_identity("databricks", {"host": "h", "extra_config": {"http_path": "/w/2"}})
    assert a != b


def test_cross_engine_differs():
    a = connection_identity("postgres", {"host": "h", "port": 5432, "database": "d"})
    b = connection_identity("mysql", {"host": "h", "port": 5432, "database": "d"})
    assert a != b


def test_identity_is_hashable():
    # Used in a set / as a dict key by the planner's co-location check.
    ident = connection_identity("postgres", {"host": "h", "port": 5432, "database": "d"})
    assert isinstance(hash(ident), int)
    assert len({ident, ident}) == 1
