"""Central provider-lookup (platform/dispatch.py) contract tests.

The dispatch module is the single choke point for selecting a per-platform
provider. It must fail closed on an unknown platform (ADR-9) and never silently
fall back to Postgres.
"""
import pytest

from workbench.backend.platform import dispatch
from workbench.backend.platform.dispatch import (
    ProviderUnavailable,
    UnknownPlatform,
    canonical_platform,
    get_connection_provider,
    get_deployment_provider,
    get_discovery_provider,
    get_query_executor,
    get_transfer_provider,
)


class TestCanonicalPlatform:
    def test_postgresql_aliases_to_postgres(self):
        assert canonical_platform("postgresql") == "postgres"
        assert canonical_platform("PostgreSQL") == "postgres"

    def test_empty_normalises_to_empty(self):
        assert canonical_platform("") == ""
        assert canonical_platform(None) == ""


class TestConnectionAndDiscovery:
    def test_all_known_platforms_resolve_a_connection_provider(self):
        for p in ("postgres", "postgresql", "mysql", "snowflake", "databricks",
                  "duckdb", "duckdb_local"):
            assert get_connection_provider(p) is not None
            assert get_discovery_provider(p) is not None

    def test_unknown_platform_fails_closed(self):
        for fn in (get_connection_provider, get_discovery_provider,
                   get_query_executor, get_deployment_provider,
                   get_transfer_provider):
            with pytest.raises(UnknownPlatform):
                fn("s3")


class TestQueryAndDeployment:
    def test_postgres_query_executor_and_deployment_provider(self):
        assert type(get_query_executor("postgres")).__name__ == "PostgresQueryExecutor"
        assert type(get_deployment_provider("postgres")).__name__ == "PostgresDeploymentProvider"

    def test_postgresql_alias_query_executor(self):
        assert type(get_query_executor("postgresql")).__name__ == "PostgresQueryExecutor"

    @pytest.mark.parametrize("platform,qe,dp", [
        ("mysql", "MySQLQueryExecutor", "MySQLDeploymentProvider"),
        ("snowflake", "SnowflakeQueryExecutor", "SnowflakeDeploymentProvider"),
        ("databricks", "DatabricksQueryExecutor", "DatabricksDeploymentProvider"),
    ])
    def test_wired_query_and_deployment_providers(self, platform, qe, dp):
        # Phase 4 wired every shipped platform's query executor + deployment
        # provider — no silent Postgres fallback, no dead SPI.
        assert type(get_query_executor(platform)).__name__ == qe
        assert type(get_deployment_provider(platform)).__name__ == dp


class TestTransfer:
    def test_relational_and_warehouse_targets_resolve(self):
        for p in ("postgres", "mysql", "duckdb", "duckdb_local", "snowflake", "databricks"):
            assert get_transfer_provider(p) is not None

    def test_object_store_fails_closed(self):
        with pytest.raises(UnknownPlatform):
            get_transfer_provider("s3")
