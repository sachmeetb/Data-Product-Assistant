"""Platform registry and provider interfaces for multi-platform support.

  - interfaces.py      — stable Protocol SPIs all providers must implement
  - registry.py        — loads manifests, resolves capabilities, dispatches providers
  - transfer_batch.py  — versioned data-plane exchange contract (TransferBatch v1)
  - migration_plan.py  — migration lifecycle state machine (MigrationPlan v1)
  - type_system.py     — canonical type system: CanonicalType enum, TypeMappingProfile,
                         four platform profiles (Postgres, MySQL, Snowflake, Databricks);
                         fail-closed get_profile() (ADR-9 compliant)
  - transform_fixtures.py — Phase 4 conformance spec: 22 golden-path SqlTransformFixture
                            entries covering all major transform DSL kinds across four
                            platforms; fixture strings use {col}/{col2} placeholders with
                            platform-specific quoting (double-quote vs backtick).
  - sql_renderer.py    — Phase 4 per-platform SQL expression renderers: BaseSqlRenderer
                         ABC + four concrete renderers (Postgres, MySQL, Snowflake,
                         Databricks); get_renderer() is fail-closed (ADR-9 compliant);
                         conformance verified against GOLDEN_PATH_FIXTURES.
  - manifests/         — YAML capability manifests (one per platform)
  - providers/         — concrete adapter implementations

The registry is the single choke point. No code outside this package
imports a vendor driver or branches on a platform name string.

Current state (Phase 4): canonical type system + 83-case conformance-tested
SQL expression renderers for Postgres, MySQL, Snowflake, and Databricks.
"""
from .registry import PlatformRegistry, get_registry
from .interfaces import (
    CapabilityLevel,
    CapabilityEvidence,
    ConnectionProvider,
    DiscoveryProvider,
    QueryExecutor,
    QueryRenderer,
    DdlRenderer,
    DeploymentProvider,
)
from .transfer_batch import (
    TransferBatch,
    PhysicalAssetRef,
    RelationalRelationRef,
    LakehouseTableRef,
    FileCollectionRef,
    FileFormat,
    OperationEncoding,
    CompletionMarker,
    ReconciliationStatus,
    ReconciliationEvidence,
    SnapshotBoundary,
    IncrementalState,
    ColumnStat,
)
from .migration_plan import (
    MigrationPlan,
    MigrationStatus,
    MigrationOwnership,
    MaintenancePolicy,
    PhaseLogEntry,
    ReconciliationRule,
    valid_next_statuses,
)
from .type_system import (
    CanonicalType,
    ColumnTypeDescriptor,
    ConversionWarningKind,
    TypeConversionWarning,
    TypeMapping,
    TypeMappingProfile,
    get_profile,
    register_profile,
    list_profiles,
)
from .transform_fixtures import (
    SqlTransformFixture,
    GOLDEN_PATH_FIXTURES,
    get_fixture,
    fixtures_for_platform,
    fixtures_for_kind,
)
from .sql_renderer import (
    BaseSqlRenderer,
    PostgresSqlRenderer,
    MysqlSqlRenderer,
    SnowflakeSqlRenderer,
    DatabricksSqlRenderer,
    get_renderer,
    list_renderers,
)

__all__ = [
    "PlatformRegistry",
    "get_registry",
    "CapabilityLevel",
    "CapabilityEvidence",
    "ConnectionProvider",
    "DiscoveryProvider",
    "QueryExecutor",
    "QueryRenderer",
    "DdlRenderer",
    "DeploymentProvider",
    # data-plane contract
    "TransferBatch",
    "PhysicalAssetRef",
    "RelationalRelationRef",
    "LakehouseTableRef",
    "FileCollectionRef",
    "FileFormat",
    "OperationEncoding",
    "CompletionMarker",
    "ReconciliationStatus",
    "ReconciliationEvidence",
    "SnapshotBoundary",
    "IncrementalState",
    "ColumnStat",
    # migration lifecycle
    "MigrationPlan",
    "MigrationStatus",
    "MigrationOwnership",
    "MaintenancePolicy",
    "PhaseLogEntry",
    "ReconciliationRule",
    "valid_next_statuses",
    # canonical type system
    "CanonicalType",
    "ColumnTypeDescriptor",
    "ConversionWarningKind",
    "TypeConversionWarning",
    "TypeMapping",
    "TypeMappingProfile",
    "get_profile",
    "register_profile",
    "list_profiles",
    # transform conformance fixtures
    "SqlTransformFixture",
    "GOLDEN_PATH_FIXTURES",
    "get_fixture",
    "fixtures_for_platform",
    "fixtures_for_kind",
    # per-platform SQL expression renderers
    "BaseSqlRenderer",
    "PostgresSqlRenderer",
    "MysqlSqlRenderer",
    "SnowflakeSqlRenderer",
    "DatabricksSqlRenderer",
    "get_renderer",
    "list_renderers",
]
