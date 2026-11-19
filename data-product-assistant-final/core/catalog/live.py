"""Live BigQuery catalog introspection.

The designed tree is continuously reconciled against what actually exists.
Neither predecessor read BigQuery schemas at all -- SILVER performed zero reads
and fabricated its "data availability" report.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

from core.model.types import CanonicalType, UnknownTypeError, normalize

#: BigQuery identifier rules: letters, digits, underscores; datasets may not be
#: quoted into the FROM clause as a parameter, so they are validated instead.
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")
_PROJECT = re.compile(r"^[A-Za-z0-9\-_.:]+$")


class InvalidIdentifierError(ValueError):
    pass


def _check_dataset(dataset: str) -> str:
    if not _IDENTIFIER.match(dataset):
        raise InvalidIdentifierError(f"unsafe dataset identifier {dataset!r}")
    return dataset


def _check_project(project: str) -> str:
    if not _PROJECT.match(project):
        raise InvalidIdentifierError(f"unsafe project identifier {project!r}")
    return project


@dataclass(frozen=True)
class LiveColumn:
    name: str
    canonical_type: CanonicalType
    nullable: bool
    description: str = ""
    raw_type: str = ""
    is_partitioning: bool = False


@dataclass
class LiveTable:
    dataset: str
    name: str
    columns: list[LiveColumn] = field(default_factory=list)
    row_count: int | None = None

    def column(self, name: str) -> LiveColumn | None:
        return next((c for c in self.columns if c.name == name), None)

    @property
    def column_names(self) -> set[str]:
        return {c.name for c in self.columns}


@dataclass
class LiveSnapshot:
    """What exists right now, across the datasets we were asked about."""

    tables: list[LiveTable] = field(default_factory=list)
    unresolved_types: list[str] = field(default_factory=list)

    def table(self, name: str, dataset: str | None = None) -> LiveTable | None:
        for t in self.tables:
            if t.name == name and (dataset is None or t.dataset == dataset):
                return t
        return None

    @property
    def table_names(self) -> set[str]:
        return {t.name for t in self.tables}


class LiveCatalog(Protocol):
    def snapshot(self, datasets: list[str]) -> LiveSnapshot: ...


_COLUMNS_QUERY = """
SELECT
  c.table_name,
  c.column_name,
  c.data_type,
  c.is_nullable,
  c.is_partitioning_column,
  fp.description
FROM `{project}`.`{dataset}`.INFORMATION_SCHEMA.COLUMNS AS c
LEFT JOIN `{project}`.`{dataset}`.INFORMATION_SCHEMA.COLUMN_FIELD_PATHS AS fp
  ON  fp.table_name  = c.table_name
  AND fp.field_path  = c.column_name
ORDER BY c.table_name, c.ordinal_position
"""


class BigQueryCatalog:
    """Reads INFORMATION_SCHEMA. Requires `google-cloud-bigquery`."""

    def __init__(self, project: str, client=None) -> None:
        self.project = _check_project(project)
        self._client = client

    def _ensure_client(self):
        if self._client is None:
            from google.cloud import bigquery  # lazy: keeps core importable offline

            self._client = bigquery.Client(project=self.project)
        return self._client

    def snapshot(self, datasets: list[str]) -> LiveSnapshot:
        client = self._ensure_client()
        snap = LiveSnapshot()

        for dataset in datasets:
            _check_dataset(dataset)
            sql = _COLUMNS_QUERY.format(project=self.project, dataset=dataset)

            by_table: dict[str, LiveTable] = {}
            for row in client.query(sql).result():
                raw_type = row["data_type"]
                try:
                    ctype = normalize(raw_type)
                except UnknownTypeError:
                    # Surface rather than default: a guessed type is how DOUBLE
                    # reached BigQuery-targeted frameworks in the first place.
                    snap.unresolved_types.append(
                        f"{dataset}.{row['table_name']}.{row['column_name']}: {raw_type}"
                    )
                    continue

                table = by_table.setdefault(
                    row["table_name"], LiveTable(dataset=dataset, name=row["table_name"])
                )
                table.columns.append(
                    LiveColumn(
                        name=row["column_name"],
                        canonical_type=ctype,
                        nullable=(row["is_nullable"] == "YES"),
                        description=row["description"] or "",
                        raw_type=raw_type,
                        is_partitioning=(row["is_partitioning_column"] == "YES"),
                    )
                )

            snap.tables.extend(by_table.values())

        return snap


class InMemoryCatalog:
    """Test/offline double. Accepts the same shape BigQueryCatalog returns."""

    def __init__(self, tables: list[LiveTable] | None = None) -> None:
        self._tables = tables or []

    def snapshot(self, datasets: list[str]) -> LiveSnapshot:
        wanted = set(datasets)
        return LiveSnapshot(
            tables=[t for t in self._tables if t.dataset in wanted]
        )
