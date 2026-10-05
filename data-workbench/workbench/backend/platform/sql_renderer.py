"""Per-platform SQL expression renderers — Phase 4 transform conformance gate.

Each renderer accepts a ``transform_kind``, ``transform_params`` dict, and a list
of positional source column names, then emits the SQL expression fragment defined
by ``GOLDEN_PATH_FIXTURES``.

Design rules (ADR-9 compliant):
- ``get_renderer(platform_id)`` is fail-closed: raises ``KeyError`` for unknown
  platforms rather than falling back to Postgres.
- Each renderer uses its platform's ``TypeMappingProfile`` for type-name resolution
  so CAST types stay consistent with the type system layer.
- Identifier quoting is platform-specific (double-quotes vs backticks).
- Transforms not yet implemented for a platform raise ``NotImplementedError``
  rather than emitting silently incorrect SQL.

Usage::

    from workbench.backend.platform.sql_renderer import get_renderer
    renderer = get_renderer("postgres")
    sql = renderer.render("cast", {"target_canonical_type": "int32"}, ["amount_str"])
    # → 'CAST("amount_str" AS INTEGER)'
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from .type_system import CanonicalType, TypeMappingProfile, get_profile


# ── base renderer ──────────────────────────────────────────────────────────────

class BaseSqlRenderer(ABC):
    """Abstract base for per-platform SQL expression renderers.

    Subclasses override ``_quote`` and may override individual ``_render_*``
    methods.  The dispatch method ``render`` calls the appropriate handler.
    """

    @property
    @abstractmethod
    def platform_id(self) -> str: ...

    def __init__(self, profile: TypeMappingProfile) -> None:
        self.profile = profile

    @abstractmethod
    def _quote(self, name: str) -> str:
        """Return a platform-quoted identifier."""

    def _col(self, columns: list[str], index: int = 0) -> str:
        return self._quote(columns[index])

    def _cast_sql_type(
        self,
        canonical: CanonicalType,
        precision: int | None = None,
        scale: int | None = None,
    ) -> str:
        """Return the SQL type name to use inside a CAST expression.

        Defaults to ``profile.map_from_canonical``; subclasses override when the
        platform's valid CAST target types differ from storage types (e.g. MySQL
        requires ``SIGNED`` instead of ``INT`` inside a CAST expression).
        """
        return self.profile.map_from_canonical(canonical, precision, scale)

    def render(
        self,
        transform_kind: str,
        transform_params: dict[str, Any],
        source_columns: list[str],
    ) -> str:
        """Dispatch to the appropriate ``_render_*`` handler.

        Raises ``NotImplementedError`` when no handler exists for the given
        ``transform_kind`` on this platform.
        """
        method = getattr(self, f"_render_{transform_kind.replace('-', '_')}", None)
        if method is None:
            raise NotImplementedError(
                f"No renderer for transform_kind {transform_kind!r} "
                f"on platform {self.platform_id!r}"
            )
        return method(transform_params, source_columns)

    # ── individual transform renderers ─────────────────────────────────────────

    def _render_cast(self, params: dict, cols: list[str]) -> str:
        col = self._col(cols)
        target = CanonicalType(params["target_canonical_type"])
        sql_type = self._cast_sql_type(
            target, params.get("precision"), params.get("scale")
        )
        return f"CAST({col} AS {sql_type})"

    def _render_concat(self, params: dict, cols: list[str]) -> str:
        raise NotImplementedError(
            f"_render_concat must be overridden for platform {self.platform_id!r}"
        )

    def _render_literal(self, params: dict, cols: list[str]) -> str:
        value = params.get("value")
        canonical_type_str = params.get("canonical_type", "string")
        if value is None:
            sql_type = self._cast_sql_type(CanonicalType(canonical_type_str))
            return f"CAST(NULL AS {sql_type})"
        if isinstance(value, str):
            return f"'{value}'"
        return str(value)

    def _render_arithmetic(self, params: dict, cols: list[str]) -> str:
        operator = params["operator"]
        safe_divide = params.get("safe_divide", False)
        col1 = self._col(cols, 0)
        col2 = self._col(cols, 1)
        if safe_divide:
            return (
                f"CASE WHEN {col2} = 0 THEN NULL "
                f"ELSE {col1} / NULLIF({col2}, 0) END"
            )
        return f"{col1} {operator} {col2}"

    def _render_coalesce(self, params: dict, cols: list[str]) -> str:
        parts = ", ".join(self._col(cols, i) for i in range(len(cols)))
        return f"COALESCE({parts})"

    def _render_case(self, params: dict, cols: list[str]) -> str:
        branches = []
        for branch in params.get("branches", []):
            when_cond = self._substitute_col_refs(branch["when"], cols)
            branches.append(f"WHEN {when_cond} THEN {branch['then']}")
        else_val = params.get("else_value", "NULL")
        return "CASE " + " ".join(branches) + f" ELSE {else_val} END"

    def _render_hash(self, params: dict, cols: list[str]) -> str:
        raise NotImplementedError(
            f"_render_hash must be overridden for platform {self.platform_id!r}"
        )

    def _render_mask(self, params: dict, cols: list[str]) -> str:
        col = self._col(cols)
        mask_kind = params.get("mask_kind", "email")
        if mask_kind == "email":
            return f"REGEXP_REPLACE({col}, '^[^@]+', '***')"
        raise NotImplementedError(
            f"mask_kind {mask_kind!r} not implemented for {self.platform_id!r}"
        )

    def _render_bucket(self, params: dict, cols: list[str]) -> str:
        col = self._col(cols)
        boundaries: list = params["boundaries"]
        labels: list[str] = params["labels"]
        # n boundaries generate n WHEN clauses.  Values below boundaries[0] fall
        # into labels[0] (same bucket as the lowest defined range) because
        # WHEN col < boundaries[0] fires first with labels[max(0, 0-1)]=labels[0].
        # For subsequent boundaries i, label = labels[i-1].
        parts = [
            f"WHEN {col} < {boundary} THEN '{labels[max(0, i - 1)]}'"
            for i, boundary in enumerate(boundaries)
        ]
        return "CASE " + " ".join(parts) + f" ELSE '{labels[-1]}' END"

    def _substitute_col_refs(self, template: str, cols: list[str]) -> str:
        """Substitute {col}/{col2} placeholders in a CASE branch condition."""
        result = template
        if cols:
            result = result.replace("{col}", self._col(cols, 0))
        if len(cols) > 1:
            result = result.replace("{col2}", self._col(cols, 1))
        return result


# ── quoting mixins ─────────────────────────────────────────────────────────────

class _DoubleQuoteMixin:
    """ANSI double-quoted identifiers (Postgres, Snowflake)."""
    def _quote(self, name: str) -> str:  # type: ignore[override]
        return f'"{name}"'


class _BacktickMixin:
    """Backtick identifiers (MySQL, Databricks / Spark SQL)."""
    def _quote(self, name: str) -> str:  # type: ignore[override]
        return f'`{name}`'


class _ConcatFunctionMixin:
    """Platforms that use CONCAT() for string concatenation.

    Postgres uses ``||`` and overrides ``_render_concat`` in its own class.
    """
    def _render_concat(self, params: dict, cols: list[str]) -> str:  # type: ignore[override]
        separator: str = params.get("separator", "")
        null_as_empty: bool = params.get("null_as_empty", False)
        col_exprs = [
            f"COALESCE({self._col(cols, i)}, '')"  # type: ignore[attr-defined]
            if null_as_empty
            else self._col(cols, i)  # type: ignore[attr-defined]
            for i in range(len(cols))
        ]
        if separator:
            args: list[str] = []
            for i, expr in enumerate(col_exprs):
                args.append(expr)
                if i < len(col_exprs) - 1:
                    args.append(f"'{separator}'")
            return f"CONCAT({', '.join(args)})"
        return f"CONCAT({', '.join(col_exprs)})"


# ── concrete renderers ─────────────────────────────────────────────────────────

class PostgresSqlRenderer(_DoubleQuoteMixin, BaseSqlRenderer):
    platform_id = "postgres"

    def _render_concat(self, params: dict, cols: list[str]) -> str:
        separator: str = params.get("separator", "")
        null_as_empty: bool = params.get("null_as_empty", False)
        parts = [
            f"COALESCE({self._col(cols, i)}, '')"
            if null_as_empty
            else self._col(cols, i)
            for i in range(len(cols))
        ]
        if separator:
            sep_lit = f"'{separator}'"
            return f" || {sep_lit} || ".join(parts)
        return " || ".join(parts)

    def _render_hash(self, params: dict, cols: list[str]) -> str:
        col = self._col(cols)
        algorithm = params.get("algorithm", "md5")
        if algorithm == "md5":
            return f"MD5(CAST({col} AS TEXT))"
        if algorithm == "sha256":
            return f"ENCODE(DIGEST(CAST({col} AS TEXT), 'sha256'), 'hex')"
        raise NotImplementedError(
            f"Hash algorithm {algorithm!r} not implemented for postgres"
        )


class MysqlSqlRenderer(_BacktickMixin, _ConcatFunctionMixin, BaseSqlRenderer):
    platform_id = "mysql"

    # MySQL's CAST() only accepts a restricted set of type keywords that differ
    # from the storage type names.  See MySQL 8.0 CAST() function reference.
    _CAST_TYPE_MAP: dict[CanonicalType, str] = {
        CanonicalType.int8:            "SIGNED",
        CanonicalType.int16:           "SIGNED",
        CanonicalType.int32:           "SIGNED",
        CanonicalType.int64:           "UNSIGNED",
        CanonicalType.float32:         "DOUBLE",
        CanonicalType.float64:         "DOUBLE",
        CanonicalType.decimal:         "DECIMAL({p},{s})",
        CanonicalType.string:          "CHAR",
        CanonicalType.binary:          "BINARY",
        CanonicalType.date:            "DATE",
        CanonicalType.time_ms:         "TIME",
        CanonicalType.timestamp_us:    "DATETIME",
        CanonicalType.timestamp_tz_us: "DATETIME",
        CanonicalType.json:            "JSON",
    }

    def _cast_sql_type(
        self,
        canonical: CanonicalType,
        precision: int | None = None,
        scale: int | None = None,
    ) -> str:
        tmpl = self._CAST_TYPE_MAP.get(canonical)
        if tmpl is None:
            return self.profile.map_from_canonical(canonical, precision, scale)
        if "{p}" in tmpl:
            p = precision or 38
            s = scale if scale is not None else 10
            return tmpl.replace("{p}", str(p)).replace("{s}", str(s))
        return tmpl

    def _render_hash(self, params: dict, cols: list[str]) -> str:
        col = self._col(cols)
        algorithm = params.get("algorithm", "md5")
        if algorithm == "md5":
            return f"MD5({col})"
        raise NotImplementedError(
            f"Hash algorithm {algorithm!r} not implemented for mysql"
        )


class SnowflakeSqlRenderer(_DoubleQuoteMixin, _ConcatFunctionMixin, BaseSqlRenderer):
    platform_id = "snowflake"

    def _render_hash(self, params: dict, cols: list[str]) -> str:
        col = self._col(cols)
        algorithm = params.get("algorithm", "md5")
        if algorithm == "md5":
            return f"MD5({col})"
        if algorithm == "sha256":
            return f"SHA2({col}, 256)"
        raise NotImplementedError(
            f"Hash algorithm {algorithm!r} not implemented for snowflake"
        )


class DatabricksSqlRenderer(_BacktickMixin, _ConcatFunctionMixin, BaseSqlRenderer):
    platform_id = "databricks"

    def _render_hash(self, params: dict, cols: list[str]) -> str:
        col = self._col(cols)
        algorithm = params.get("algorithm", "md5")
        if algorithm == "md5":
            return f"MD5(CAST({col} AS STRING))"
        if algorithm == "sha256":
            return f"SHA2(CAST({col} AS STRING), 256)"
        raise NotImplementedError(
            f"Hash algorithm {algorithm!r} not implemented for databricks"
        )


# ── renderer registry ──────────────────────────────────────────────────────────

_RENDERER_CLASSES: dict[str, type[BaseSqlRenderer]] = {
    "postgres":   PostgresSqlRenderer,
    "mysql":      MysqlSqlRenderer,
    "snowflake":  SnowflakeSqlRenderer,
    "databricks": DatabricksSqlRenderer,
}

_RENDERER_INSTANCES: dict[str, BaseSqlRenderer] = {
    pid: cls(get_profile(pid))
    for pid, cls in _RENDERER_CLASSES.items()
}


def get_renderer(platform_id: str) -> BaseSqlRenderer:
    """Return the ``BaseSqlRenderer`` for ``platform_id``.

    Raises ``KeyError`` for unknown platforms (fail-closed; no Postgres fallback).
    """
    if platform_id not in _RENDERER_INSTANCES:
        raise KeyError(
            f"No SQL renderer registered for platform {platform_id!r}. "
            f"Known platforms: {sorted(_RENDERER_INSTANCES)}"
        )
    return _RENDERER_INSTANCES[platform_id]


def list_renderers() -> list[str]:
    """Return all registered renderer platform IDs (sorted)."""
    return sorted(_RENDERER_INSTANCES)
