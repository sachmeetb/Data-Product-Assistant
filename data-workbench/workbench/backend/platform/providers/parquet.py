"""Parquet / DuckDB-local source discovery provider.

Implements ConnectionProvider + DiscoveryProvider for a directory of Parquet
files, using DuckDB as the read engine (`DESCRIBE`, `read_parquet`). This is the
input side of the lakehouse pattern: register a `duckdb_local` connection over a
directory + optional glob, bind it to a project, and run discovery/profiling —
the DCAT/DQV graph loaders consume the emitted YAML unchanged.

The connection carries a base directory (+ optional file glob), NOT host/port/
credentials — it's a filesystem source, not a networked database.

duckdb is imported lazily inside method bodies so the module loads in test
environments without the driver.
"""
from __future__ import annotations

import glob as _glob
import logging
from pathlib import Path
from typing import Any, Optional

from ..interfaces import (
    CapabilityEvidence,
    CapabilityLevel,
    ColumnInfo,
    DiscoveryBundle,
    ManagedConnection,
    NamespaceRef,
    RelationSummary,
    ValidationReport,
)

logger = logging.getLogger(__name__)

PLATFORM_TYPE = "duckdb_local"


def _base_dir(connection_ref: dict[str, Any]) -> str:
    """Resolve the base directory from a connection reference."""
    extra = connection_ref.get("extra_config") or {}
    return (
        extra.get("dir")
        or connection_ref.get("database")
        or connection_ref.get("host")
        or ""
    )


def _file_glob(connection_ref: dict[str, Any]) -> str:
    extra = connection_ref.get("extra_config") or {}
    return extra.get("glob") or "*.parquet"


class ParquetConnectionProvider:
    """Validates config and probes a Parquet directory."""

    def validate_config(
        self, public_config: dict[str, Any], secret_ref: str
    ) -> ValidationReport:
        errors = []
        extra = public_config.get("extra_config") or {}
        base = extra.get("dir") or public_config.get("database") or public_config.get("host")
        if not base:
            errors.append("a base directory is required (extra_config.dir / database)")
        return ValidationReport(valid=not errors, errors=errors)

    def probe(self, connection_ref: dict[str, Any]) -> CapabilityEvidence:
        evidence = CapabilityEvidence(
            platform_type=PLATFORM_TYPE,
            instance_id=connection_ref.get("connection_id", "unknown"),
        )
        base = _base_dir(connection_ref)
        if base and Path(base).exists():
            evidence.capabilities = {
                "connection": CapabilityLevel.PREVIEW,
                "discovery": CapabilityLevel.PREVIEW,
            }
        else:
            evidence.warnings.append(f"directory not found: {base!r}")
        return evidence

    def open(self, connection_ref: dict[str, Any], purpose: str) -> ManagedConnection:
        import duckdb
        conn = duckdb.connect()
        return ManagedConnection(
            platform_type=PLATFORM_TYPE,
            instance_id=connection_ref.get("connection_id", "unknown"),
            purpose=purpose,
            _internal=conn,
        )


class ParquetDiscoveryProvider:
    """Lists directories (namespaces) and Parquet files (relations); describes
    each via DuckDB DESCRIBE."""

    def list_namespaces(
        self,
        connection_ref: dict[str, Any],
        parent: Optional[str] = None,
    ) -> list[NamespaceRef]:
        base = Path(_base_dir(connection_ref))
        instance_id = connection_ref.get("connection_id", "unknown")
        results: list[NamespaceRef] = []
        if not base.exists():
            return results
        root = base / parent if parent else base
        # Immediate subdirectories are namespaces; the root itself is always one.
        results.append(NamespaceRef(
            platform_instance_id=instance_id, parts=[str(base)], labels={"dir": str(base)},
        ))
        try:
            for child in sorted(p for p in root.iterdir() if p.is_dir()):
                results.append(NamespaceRef(
                    platform_instance_id=instance_id,
                    parts=[str(child)], labels={"dir": str(child)},
                ))
        except OSError as exc:
            logger.warning("parquet list_namespaces failed: %s", exc)
        return results

    def list_relations(
        self,
        connection_ref: dict[str, Any],
        namespace: NamespaceRef,
    ) -> list[RelationSummary]:
        import datetime
        directory = namespace.parts[0] if namespace.parts else _base_dir(connection_ref)
        pattern = _file_glob(connection_ref)
        results: list[RelationSummary] = []
        for path in sorted(_glob.glob(str(Path(directory) / pattern))):
            p = Path(path)
            stem = p.stem
            size_bytes = None
            row_count = None
            last_modified = None
            try:
                st = p.stat()
                size_bytes = st.st_size
                last_modified = datetime.datetime.utcfromtimestamp(st.st_mtime).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
            except OSError:
                pass
            try:
                import pyarrow.parquet as pq
                pf = pq.ParquetFile(path)
                row_count = pf.metadata.num_rows
            except Exception:
                pass
            results.append(RelationSummary(
                namespace=namespace,
                name=stem,
                relation_kind="file",
                row_count=row_count,
                size_bytes=size_bytes,
                last_modified=last_modified,
            ))
        return results

    def describe_relations(
        self,
        connection_ref: dict[str, Any],
        refs: list[RelationSummary],
    ) -> DiscoveryBundle:
        import duckdb
        instance_id = connection_ref.get("connection_id", "unknown")
        pattern = _file_glob(connection_ref)
        relations: list[dict[str, Any]] = []
        warnings: list[str] = []
        con = duckdb.connect()
        try:
            for ref in refs:
                directory = ref.namespace.parts[0] if ref.namespace.parts else _base_dir(connection_ref)
                # Resolve the concrete file for this relation name.
                candidates = _glob.glob(str(Path(directory) / pattern))
                path = next((c for c in candidates if Path(c).stem == ref.name), None)
                if path is None:
                    warnings.append(f"{ref.name}: file not found")
                    continue
                try:
                    cols = con.execute(
                        f"DESCRIBE SELECT * FROM read_parquet('{path}')"
                    ).fetchall()
                    row_count = con.execute(
                        f"SELECT count(*) FROM read_parquet('{path}')"
                    ).fetchone()[0]
                    relations.append({
                        "schema": str(Path(directory).name) or "parquet",
                        "table": ref.name,
                        "comment": "",
                        "row_count": int(row_count),
                        "source_file": path,
                        "columns": [
                            {"name": c[0], "data_type": c[1], "is_nullable": (c[2] == "YES")}
                            for c in cols
                        ],
                        "primary_keys": [],
                        "foreign_keys": [],
                        "unique_constraints": [],
                        "indexes": [],
                        "check_constraints": [],
                    })
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"{ref.name}: {exc}")
        finally:
            con.close()
        return DiscoveryBundle(
            platform_type=PLATFORM_TYPE,
            platform_instance_id=instance_id,
            relations=relations,
            warnings=warnings,
        )

    def list_columns(
        self,
        connection_ref: dict[str, Any],
        relation: RelationSummary,
    ) -> list[ColumnInfo]:
        # Parquet is a file source, not a deployed-SQL-view serving target, so
        # the NL→SQL view-column hydration path never calls this. Satisfy the
        # DiscoveryProvider contract via a best-effort DuckDB DESCRIBE.
        bundle = self.describe_relations(connection_ref, [relation])
        out: list[ColumnInfo] = []
        for rel in bundle.relations:
            for c in rel.get("columns", []):
                out.append(ColumnInfo(
                    name=c.get("name", ""),
                    data_type=c.get("data_type", ""),
                    nullable=bool(c.get("is_nullable", True)),
                ))
        return out
