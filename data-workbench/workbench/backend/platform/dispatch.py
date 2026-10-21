"""Central provider lookup — the single choke point for selecting a
per-platform provider (connection / discovery / query / deployment / transfer).

This module lives OUTSIDE any router so every code path resolves a provider the
same way.  It is the in-process peer of ``registry.py`` (which owns capability
manifests): the registry answers *"is this capability usable?"*, this module
answers *"which object implements it for this platform?"*.

Fail-closed on an unknown platform (mirrors ``registry.assert_usable``, ADR-9):
requesting a provider for a platform with no adapter raises ``UnknownPlatform``
rather than silently falling back to Postgres.  A KNOWN platform that simply
hasn't implemented a given provider *kind* yet raises ``ProviderUnavailable``.

No vendor driver is imported at module load — each getter lazy-imports the
concrete provider module so an absent driver (or an absent optional platform)
never breaks importing this module.
"""
from __future__ import annotations

# platform-type aliases → canonical id (the SAME normalisation the registry and
# the resolvers use: "postgresql" is an alias for "postgres").
_ALIASES = {"postgresql": "postgres"}


def canonical_platform(platform_type: str) -> str:
    """Normalise a platform_type string to its canonical id (lower-cased,
    aliases collapsed).  An empty/None value normalises to ""."""
    p = (platform_type or "").strip().lower()
    return _ALIASES.get(p, p)


class UnknownPlatform(Exception):
    """Requested a provider for a platform_type with no adapter at all.

    Callers should convert this to HTTP 422 / MCP capability_unavailable —
    never catch and silently fall back to another platform.
    """


class ProviderUnavailable(Exception):
    """The platform is known but has no provider of the requested KIND yet
    (e.g. a query executor for a discovery-only platform)."""


# ── connection ─────────────────────────────────────────────────────────────────

def get_connection_provider(platform_type: str):
    """Return the ConnectionProvider instance for ``platform_type``.

    Fail-closed: an unknown platform raises ``UnknownPlatform``.
    """
    p = canonical_platform(platform_type)
    if p == "postgres":
        from .providers.postgres import PostgresConnectionProvider
        return PostgresConnectionProvider()
    if p == "mysql":
        from .providers.mysql import MySQLConnectionProvider
        return MySQLConnectionProvider()
    if p == "snowflake":
        from .providers.snowflake import SnowflakeConnectionProvider
        return SnowflakeConnectionProvider()
    if p == "databricks":
        from .providers.databricks import DatabricksConnectionProvider
        return DatabricksConnectionProvider()
    if p in ("duckdb", "duckdb_local"):
        from .providers.parquet import ParquetConnectionProvider
        return ParquetConnectionProvider()
    raise UnknownPlatform(
        f"No connection provider for platform '{platform_type}'. "
        f"Known: {sorted(_KNOWN_PLATFORMS)}"
    )


# ── discovery ────────────────────────────────────────────────────────────────

def get_discovery_provider(platform_type: str):
    """Return the DiscoveryProvider instance for ``platform_type``.

    Fail-closed: an unknown platform raises ``UnknownPlatform``.
    """
    p = canonical_platform(platform_type)
    if p == "postgres":
        from .providers.postgres import PostgresDiscoveryProvider
        return PostgresDiscoveryProvider()
    if p == "mysql":
        from .providers.mysql import MySQLDiscoveryProvider
        return MySQLDiscoveryProvider()
    if p == "snowflake":
        from .providers.snowflake import SnowflakeDiscoveryProvider
        return SnowflakeDiscoveryProvider()
    if p == "databricks":
        from .providers.databricks import DatabricksDiscoveryProvider
        return DatabricksDiscoveryProvider()
    if p in ("duckdb", "duckdb_local"):
        from .providers.parquet import ParquetDiscoveryProvider
        return ParquetDiscoveryProvider()
    raise UnknownPlatform(
        f"No discovery provider for platform '{platform_type}'. "
        f"Known: {sorted(_KNOWN_PLATFORMS)}"
    )


# ── query execution ────────────────────────────────────────────────────────────

def get_query_executor(platform_type: str):
    """Return the QueryExecutor instance for ``platform_type``.

    Fail-closed: an unknown platform raises ``UnknownPlatform``; a known
    platform without a query executor raises ``ProviderUnavailable``.
    """
    p = canonical_platform(platform_type)
    if p == "postgres":
        from .providers.postgres import PostgresQueryExecutor
        return PostgresQueryExecutor()
    if p == "mysql":
        from .providers.mysql import MySQLQueryExecutor
        return MySQLQueryExecutor()
    if p == "snowflake":
        from .providers.snowflake import SnowflakeQueryExecutor
        return SnowflakeQueryExecutor()
    if p == "databricks":
        from .providers.databricks import DatabricksQueryExecutor
        return DatabricksQueryExecutor()
    if p not in _KNOWN_PLATFORMS:
        raise UnknownPlatform(
            f"No query executor for platform '{platform_type}'. "
            f"Known: {sorted(_KNOWN_PLATFORMS)}"
        )
    raise ProviderUnavailable(
        f"Platform '{platform_type}' has no query executor yet."
    )


# ── deployment ─────────────────────────────────────────────────────────────────

def get_deployment_provider(platform_type: str):
    """Return the DeploymentProvider instance for ``platform_type``.

    Fail-closed: an unknown platform raises ``UnknownPlatform``; a known
    platform without a deployment provider raises ``ProviderUnavailable``.
    """
    p = canonical_platform(platform_type)
    if p == "postgres":
        from .providers.postgres import PostgresDeploymentProvider
        return PostgresDeploymentProvider()
    if p == "mysql":
        from .providers.mysql import MySQLDeploymentProvider
        return MySQLDeploymentProvider()
    if p == "snowflake":
        from .providers.snowflake import SnowflakeDeploymentProvider
        return SnowflakeDeploymentProvider()
    if p == "databricks":
        from .providers.databricks import DatabricksDeploymentProvider
        return DatabricksDeploymentProvider()
    if p not in _KNOWN_PLATFORMS:
        raise UnknownPlatform(
            f"No deployment provider for platform '{platform_type}'. "
            f"Known: {sorted(_KNOWN_PLATFORMS)}"
        )
    raise ProviderUnavailable(
        f"Platform '{platform_type}' has no view-deployment provider yet "
        "(direct virtual-view deploy is not supported for this platform; "
        "use the dbt materialization path)."
    )


# ── cross-platform transfer ──────────────────────────────────────────────────

def get_transfer_provider(platform_type: str):
    """Return the TransferExecutionProvider for a TARGET ``platform_type``.

    The DuckDB engine handles postgres / mysql / duckdb targets; the dlt engine
    handles warehouse targets (snowflake / databricks).  Fail-closed: an unknown
    platform raises ``UnknownPlatform``.
    """
    p = canonical_platform(platform_type)
    if p in ("postgres", "mysql", "duckdb", "duckdb_local", "snowflake", "databricks"):
        from .providers.transfer import DuckDBTransferProvider
        return DuckDBTransferProvider()
    raise UnknownPlatform(
        f"No transfer provider for platform '{platform_type}'. "
        f"Known: {sorted(_KNOWN_PLATFORMS)}"
    )


# ── object store (ADR-14) ──────────────────────────────────────────────────────

# Object-store platforms are a SEPARATE family from the SQL platforms above:
# they have no query executor / deployment provider, so they are deliberately
# kept out of _KNOWN_PLATFORMS (the SQL resolvers keep raising UnknownPlatform
# for them, which is correct — there is no SELECT for a bucket).
_OBJECT_STORE_PLATFORMS = frozenset({"s3", "gcs", "azure_adls"})


def get_object_store_provider(platform_type: str):
    """Return the ObjectStoreProvider for ``platform_type`` (ADR-14).

    Fail-closed like the SQL resolvers: an unknown platform raises
    ``UnknownPlatform``; a known object-store platform whose provider isn't
    implemented yet raises ``ProviderUnavailable``.
    """
    p = canonical_platform(platform_type)
    if p == "s3":
        from .providers.object_store_s3 import S3ObjectStoreProvider
        return S3ObjectStoreProvider()
    if p in _OBJECT_STORE_PLATFORMS:
        raise ProviderUnavailable(
            f"Object-store platform '{platform_type}' has no provider yet "
            "(only 's3' is implemented; gcs/azure_adls are registered but not built)."
        )
    raise UnknownPlatform(
        f"No object-store provider for platform '{platform_type}'. "
        f"Known object stores: {sorted(_OBJECT_STORE_PLATFORMS)}"
    )


# ── code assets ──────────────────────────────────────────────────────────────

def get_code_asset_provider(platform_type: str):
    """Return the CodeAssetProvider for ``platform_type``.

    Fail-closed: unknown platform → UnknownPlatform; known platform with no
    code-asset support → ProviderUnavailable (postgres / mysql / duckdb families).
    """
    p = canonical_platform(platform_type)
    if p == "snowflake":
        from .providers.snowflake import SnowflakeCodeAssetProvider
        return SnowflakeCodeAssetProvider()
    if p == "databricks":
        from .providers.databricks import DatabricksCodeAssetProvider
        return DatabricksCodeAssetProvider()
    if p not in _KNOWN_PLATFORMS:
        raise UnknownPlatform(
            f"No code-asset provider for platform '{platform_type}'. "
            f"Known: {sorted(_KNOWN_PLATFORMS)}"
        )
    raise ProviderUnavailable(
        f"Platform '{platform_type}' has no code-asset provider "
        "(postgres/mysql/duckdb do not expose tasks/notebooks)."
    )


# Every platform_type this dispatcher knows about (canonicalised).  Used only for
# fail-closed error messages — the getters above are the source of truth.
_KNOWN_PLATFORMS = frozenset(
    {"postgres", "mysql", "snowflake", "databricks", "duckdb", "duckdb_local"}
)
