"""Platform-owned namespace / relation-qualification model.

The third platform capability alongside ``type_system`` (canonical types) and
``sql_renderer`` (expression SQL): how a platform *addresses* a relation. This
is the single source of truth for namespace depth and relation qualification so
no platform-specific assumption ("Databricks is 3-level", "a source catalog
needs USE CATALOG", "split the database field on '.'") leaks into shared code.

Data comes from the platform manifest's ``namespace`` block (already declared,
e.g. ``parts: [catalog, schema, relation]`` + optional ``sessionCatalogStatement``).
Quoting delegates to :mod:`sql_ident` (the one quote-char source). ``get_namespace_model``
is fail-closed per ADR-9 — unknown platforms raise, never silently assume Postgres.

Runner boundary: ``serving_runners/`` is stdlib-only and CANNOT import this. So
:meth:`NamespaceModel.to_descriptor` serialises the model into ``package.json``;
the runner applies the descriptor generically (see ``_deploy_core._namespace_ops``).
"""
from __future__ import annotations

from dataclasses import dataclass

from ..sql_ident import (
    quote_ident, quote_created_relation, quote_style, upper_folds, fold_created_ident,
)
from .registry import get_registry


@dataclass(frozen=True)
class NamespaceModel:
    """How a platform addresses a relation. Built from the manifest."""

    platform_id: str
    parts: list[str]                 # ordered, e.g. ["catalog", "schema", "relation"]
    session_catalog_statement: str   # template with {catalog}, or "" when none

    # ── shape ────────────────────────────────────────────────────────────────
    @property
    def levels(self) -> int:
        """Total addressing depth INCLUDING the relation (2 = schema.table)."""
        return len(self.parts)

    @property
    def container_parts(self) -> list[str]:
        """The namespace parts that CONTAIN a relation (everything but the last)."""
        return list(self.parts[:-1])

    # ── quoting (delegates to the one quote-char source) ─────────────────────
    # These emit references to objects CREATEd by unquoted DDL (the deployed
    # views), so they fold each identifier to the platform's natural unquoted case
    # (UPPER on Snowflake) before quoting — keeping the CREATE header, the runner
    # smoke test (which mirrors this via the descriptor), and every read
    # (sql_ident.quote_created_relation) on ONE case. Identity on non-upper-
    # folding platforms.
    def quote_ident(self, name: str) -> str:
        return quote_ident(fold_created_ident(name, self.platform_id), self.platform_id)

    def qualify(self, namespace: str | None, relation: str) -> str:
        """Fully-qualified, per-platform-quoted ``namespace.relation``.

        ``namespace`` may be a dotted string ("catalog.schema"); each segment is
        quoted independently. Reuses :func:`sql_ident.quote_created_relation` so
        an unquoted-created object on an upper-folding platform is addressed UPPER.
        """
        return quote_created_relation(namespace, relation, self.platform_id)

    # ── parsing ──────────────────────────────────────────────────────────────
    def parse(self, raw: str | None) -> dict[str, str]:
        """Split a dotted container namespace ("samples.bakehouse") into a
        ``{part_name: value}`` map, RIGHT-aligned to the platform's container
        parts. Fewer dotted parts fill the innermost slots first:
          databricks ("catalog","schema"):  "a.b" → {catalog:a, schema:b};
                                             "b"   → {schema:b}
          postgres   ("schema",):            "public" → {schema:public}
        The relation part is never produced here.
        """
        containers = self.container_parts
        values = [p for p in (raw or "").split(".") if p]
        if not values or not containers:
            return {}
        values = values[-len(containers):]          # right-align, drop overflow
        offset = len(containers) - len(values)
        return {containers[offset + i]: v for i, v in enumerate(values)}

    def catalog_of(self, raw: str | None) -> str:
        """The catalog value from a dotted namespace, or "" when the platform
        has no catalog level / none was given."""
        return self.parse(raw).get("catalog", "")

    # ── deploy statements ────────────────────────────────────────────────────
    def session_setup_statements(self, source_namespace: str | None) -> list[str]:
        """Statements to run before creating a view so unqualified body refs
        resolve against the SOURCE. Empty unless the platform declares a
        ``sessionCatalogStatement`` AND a source catalog is resolvable."""
        if not self.session_catalog_statement:
            return []
        catalog = self.catalog_of(source_namespace)
        if not catalog:
            return []
        return [self.session_catalog_statement.format(catalog=self.quote_ident(catalog))]

    def create_namespace_statement(self, namespace: str | None) -> str | None:
        """`CREATE SCHEMA IF NOT EXISTS <quoted container namespace>` for the
        target, or None when there's no container to create."""
        parsed = self.parse(namespace)
        if not parsed:
            return None
        quoted = ".".join(self.quote_ident(v) for v in parsed.values())
        return f"CREATE SCHEMA IF NOT EXISTS {quoted}"

    # ── serialisation for the stdlib-only runner ─────────────────────────────
    def to_descriptor(self) -> dict:
        """A JSON-safe descriptor embedded in the deploy package's
        ``package.json`` so the runner reproduces quoting + session-setup +
        create-namespace WITHOUT importing this module."""
        return {
            "platform": self.platform_id,
            "parts": list(self.parts),
            "quote_style": quote_style(self.platform_id),
            # Snowflake folds unquoted DDL to UPPER; the runner upper-folds its
            # smoke-test / create-namespace identifiers to match the UPPER view
            # the CREATE header (also UPPER) makes.
            "upper_fold": upper_folds(self.platform_id),
            "session_catalog_statement": self.session_catalog_statement,
        }


def get_namespace_model(platform_id: str) -> NamespaceModel:
    """Return the NamespaceModel for a platform. Fail-closed (ADR-9): unknown
    platforms raise ``KeyError`` rather than defaulting to Postgres.

    Postgres aliases (``postgresql``) resolve to the ``postgres`` manifest.
    Platforms without a manifest but with a well-known 2-level shape (``ansi``,
    ``duckdb``, ``bigquery`` until it ships a manifest) fall back to a schema.
    """
    pid = (platform_id or "").strip().lower()
    if pid == "postgresql":
        pid = "postgres"
    reg = get_registry()
    manifest = reg.get_manifest(pid)
    if manifest is not None:
        ns = manifest.namespace or {}
        parts = list(ns.get("parts") or ["schema", "relation"])
        stmt = ns.get("sessionCatalogStatement", "") or ""
        return NamespaceModel(platform_id=pid, parts=parts, session_catalog_statement=stmt)
    # Manifest-less engines used internally by the compiler/sampler. These are
    # deterministic 2-level (schema.relation) with no session catalog.
    if pid in ("ansi", "duckdb", "bigquery"):
        return NamespaceModel(platform_id=pid, parts=["schema", "relation"],
                              session_catalog_statement="")
    raise KeyError(
        f"No namespace model for platform '{platform_id}'. Add a platform manifest "
        f"(workbench/backend/platform/manifests/{pid}.yaml) with a 'namespace' block."
    )
