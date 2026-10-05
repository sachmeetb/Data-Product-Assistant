#!/usr/bin/env python3
"""Deterministic, offline metadata + volumetrics + guarded-profiling extractor.

STDLIB + the one DB driver only — this file is copied verbatim into every
downloadable extraction package and must run on a bare Python 3.8+ with no
third-party imports beyond the package's ``requirements.txt`` (its DB driver,
``pyyaml``, and the picker lib used only by ``run.py``). It must NOT import
anything from ``workbench.*`` and contains **no LLM/agent** — pure deterministic
introspection.

The extraction logic is PORTED (not imported) from the backend's platform
providers (``platform/providers/{postgres,mysql,snowflake,databricks}.py``) plus
the ``data-discovery``/``data-profiling`` skill scripts. Every query is read-only
(``information_schema`` / ``SHOW`` / ``DESCRIBE`` / bounded aggregate SELECTs); the
profiler NEVER dumps full tables and redacts value-bearing outputs for
PII-classified or over-cap columns.
"""
from __future__ import annotations

import os
import re
from typing import Any, Optional

# ── column-value classification (for profiling query shape) ───────────────────

_NUMERIC_TOKENS = ("int", "numeric", "decimal", "real", "double", "float",
                   "serial", "money", "number", "bigint", "smallint", "tinyint")
_DATE_TOKENS = ("date", "timestamp", "time", "interval", "datetime")
_TEXT_TOKENS = ("char", "text", "string", "varchar", "clob", "citext", "name")
_BOOL_TOKENS = ("bool",)


def value_kind(data_type: str) -> str:
    t = (data_type or "").lower().strip()
    if any(tok in t for tok in _BOOL_TOKENS):
        return "boolean"
    if any(tok in t for tok in _DATE_TOKENS):
        return "date"
    if any(tok in t for tok in _NUMERIC_TOKENS):
        return "numeric"
    if any(tok in t for tok in _TEXT_TOKENS):
        return "text"
    return "other"


# ── PII heuristic (ported from estate._PII_TOKENS / classify_column) ──────────

_DEFAULT_PII_TOKENS = (
    "pan", "aadhaar", "email", "mobile", "phone", "ssn", "dob",
    "card_number", "account_number", "ip_address", "passport", "password",
    "secret", "token", "address",
)


def is_pii(name: str, tokens=_DEFAULT_PII_TOKENS) -> bool:
    low = (name or "").lower()
    return any(t in low for t in tokens)


# ══════════════════════════════════════════════════════════════════════════════
# Extractor base + per-platform implementations
# ══════════════════════════════════════════════════════════════════════════════

class Extractor:
    """Common contract; each platform overrides connect + the read queries.

    A relation dict is ``{name, relation_kind, row_count, row_count_is_estimate,
    size_bytes, last_modified, num_files, comment}``; a column dict is
    ``{name, data_type, nullable, ordinal, comment, primary_key}``; an FK dict is
    ``{from_table, from_column, to_schema, to_table, to_column}``.
    """
    platform = "generic"
    quote_char = '"'

    def __init__(self, env: dict[str, str]):
        self.env = env
        self._conn = None

    # subclasses implement ------------------------------------------------------
    def connect(self):  # pragma: no cover - driver-specific
        raise NotImplementedError

    def list_schemas(self) -> list[str]:  # pragma: no cover
        raise NotImplementedError

    def list_relations(self, schema: str) -> list[dict[str, Any]]:  # pragma: no cover
        raise NotImplementedError

    def list_columns(self, schema: str, table: str) -> list[dict[str, Any]]:  # pragma: no cover
        raise NotImplementedError

    def list_foreign_keys(self, schema: str) -> list[dict[str, Any]]:  # pragma: no cover
        return []

    # shared --------------------------------------------------------------------
    def q(self, ident: str) -> str:
        c = self.quote_char
        return c + str(ident).replace(c, c + c) + c

    def qualified(self, schema: str, table: str) -> str:
        return f"{self.q(schema)}.{self.q(table)}"

    def cursor(self):
        return self._conn.cursor()

    def close(self):
        try:
            if self._conn:
                self._conn.close()
        except Exception:
            pass

    # ── guarded profiling (dialect-portable core) ──────────────────────────────
    def profile_column(self, schema: str, table: str, col: dict[str, Any],
                        sample_limit: int, top_n: int) -> Optional[dict[str, Any]]:
        """Profile one column with a single bounded aggregate pass + an optional
        top-N pass. Returns a compact profile dict (never raw rows). Best-effort:
        any failure returns None so one bad column never sinks the run."""
        name = col["name"]
        kind = value_kind(col.get("data_type", ""))
        colq = self.q(name)
        rel = self.qualified(schema, table)
        length_fn = self._length_fn()
        try:
            with self.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) FROM {rel}")
                total = int(cur.fetchone()[0] or 0)
                from_clause = f"FROM {rel}"
                sampled = False
                if sample_limit and total > sample_limit:
                    from_clause = f"FROM (SELECT * FROM {rel} {self._limit(sample_limit)}) _wb_s"
                    sampled = True
                sels = [f"COUNT({colq}) AS non_null",
                        f"COUNT(DISTINCT {colq}) AS distinct_count"]
                if kind == "numeric":
                    sels += [f"MIN({colq}) AS mn", f"MAX({colq}) AS mx",
                             f"AVG({self._numeric_cast(colq)}) AS mean"]
                elif kind == "date":
                    sels += [f"MIN({colq}) AS mn", f"MAX({colq}) AS mx"]
                elif kind == "text":
                    sels += [f"MIN({length_fn}({colq})) AS min_len",
                             f"MAX({length_fn}({colq})) AS max_len",
                             f"AVG({self._numeric_cast(length_fn + '(' + colq + ')')}) AS avg_len"]
                cur.execute(f"SELECT COUNT(*) AS scanned, {', '.join(sels)} {from_clause}")
                row = cur.fetchone()
                names = [d[0].lower() for d in cur.description]
                rec = dict(zip(names, row))
            scanned = int(rec.get("scanned") or 0)
            non_null = int(rec.get("non_null") or 0)
            distinct_count = int(rec.get("distinct_count") or 0)
            null_count = max(0, scanned - non_null)
            profile: dict[str, Any] = {
                "null_count": null_count,
                "null_rate": round(null_count / scanned, 6) if scanned else 0.0,
                "distinct_count": distinct_count,
            }
            if kind == "numeric":
                profile["min"] = _num(rec.get("mn"))
                profile["max"] = _num(rec.get("mx"))
                profile["mean"] = _num(rec.get("mean"))
            elif kind == "date":
                profile["min"] = _asdate(rec.get("mn"))
                profile["max"] = _asdate(rec.get("mx"))
            elif kind == "text":
                profile["min_length"] = _int(rec.get("min_len"))
                profile["max_length"] = _int(rec.get("max_len"))
                profile["avg_length"] = _num(rec.get("avg_len"))
            # top values for low-card categoricals only (never free-text dumps).
            wants_top = kind in ("text", "boolean", "other") or (
                kind == "numeric" and 0 < distinct_count <= top_n * 2)
            if wants_top:
                profile["top_values"] = self._top_values(
                    schema, table, name, from_clause, scanned, top_n)
            profile["_kind"] = kind
            profile["_sampled"] = sampled
            profile["_distinct"] = distinct_count
            return profile
        except Exception:
            self._rollback()
            return None

    def _top_values(self, schema, table, name, from_clause, scanned, top_n) -> list[dict]:
        colq = self.q(name)
        out: list[dict] = []
        try:
            with self.cursor() as cur:
                cur.execute(
                    f"SELECT {self._to_text(colq)} AS v, COUNT(*) AS c {from_clause} "
                    f"WHERE {colq} IS NOT NULL GROUP BY {colq} ORDER BY c DESC {self._limit(top_n)}")
                denom = scanned or 1
                for v, c in cur.fetchall():
                    out.append({"value": v, "count": int(c),
                                "frequency": round(int(c) / denom, 6)})
        except Exception:
            self._rollback()
        return out

    # dialect hooks (overridable) ----------------------------------------------
    def _limit(self, n: int) -> str:
        return f"LIMIT {int(n)}"

    def _length_fn(self) -> str:
        return "LENGTH"

    def _numeric_cast(self, expr: str) -> str:
        return f"CAST({expr} AS DECIMAL(38,6))"

    def _to_text(self, expr: str) -> str:
        return f"CAST({expr} AS CHAR)"

    def _rollback(self):
        try:
            self._conn.rollback()
        except Exception:
            pass


# ── Postgres ───────────────────────────────────────────────────────────────────

class PostgresExtractor(Extractor):
    platform = "postgres"
    quote_char = '"'
    _SYSTEM = ("information_schema", "pg_catalog", "pg_toast",
               "pg_temp_1", "pg_toast_temp_1")

    def connect(self):
        import psycopg2
        self._conn = psycopg2.connect(_pg_dsn(self.env))
        self._conn.autocommit = True
        return self._conn

    def list_schemas(self):
        with self.cursor() as cur:
            cur.execute("SELECT schema_name FROM information_schema.schemata "
                        "WHERE schema_name NOT IN %s ORDER BY schema_name", (self._SYSTEM,))
            return [r[0] for r in cur.fetchall()]

    def list_relations(self, schema):
        out = []
        with self.cursor() as cur:
            cur.execute(
                """
                SELECT t.table_name, t.table_type,
                       c.reltuples::bigint AS row_estimate,
                       pg_total_relation_size(c.oid) AS total_bytes,
                       obj_description(c.oid) AS comment
                FROM information_schema.tables t
                LEFT JOIN pg_namespace n ON n.nspname = t.table_schema
                LEFT JOIN pg_class c ON c.relname = t.table_name AND c.relnamespace = n.oid
                WHERE t.table_schema = %s ORDER BY t.table_name
                """, (schema,))
            for name, kind, row_est, total_bytes, comment in cur.fetchall():
                rel_kind = ("view" if kind == "VIEW"
                            else "materialized_view" if kind == "MATERIALIZED VIEW" else "table")
                rc = int(row_est) if row_est is not None and row_est >= 0 else None
                out.append({
                    "name": name, "relation_kind": rel_kind, "row_count": rc,
                    "row_count_is_estimate": rc is not None,
                    "size_bytes": int(total_bytes) if total_bytes is not None else None,
                    "last_modified": None, "num_files": None,
                    "comment": comment or None,
                })
        return out

    def list_columns(self, schema, table):
        pks = self._primary_keys(schema, table)
        out = []
        with self.cursor() as cur:
            cur.execute(
                """
                SELECT c.column_name, c.data_type, c.is_nullable, c.ordinal_position,
                       pgd.description
                FROM information_schema.columns c
                LEFT JOIN pg_catalog.pg_statio_all_tables st
                    ON c.table_schema = st.schemaname AND c.table_name = st.relname
                LEFT JOIN pg_catalog.pg_description pgd
                    ON pgd.objoid = st.relid AND pgd.objsubid = c.ordinal_position
                WHERE c.table_schema = %s AND c.table_name = %s
                ORDER BY c.ordinal_position
                """, (schema, table))
            for name, dtype, nullable, ordinal, comment in cur.fetchall():
                out.append({"name": name, "data_type": dtype,
                            "nullable": nullable == "YES", "ordinal": ordinal,
                            "comment": comment or None, "primary_key": name in pks})
        return out

    def _primary_keys(self, schema, table) -> set:
        with self.cursor() as cur:
            cur.execute(
                """
                SELECT kcu.column_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                  ON tc.constraint_name = kcu.constraint_name
                 AND tc.table_schema = kcu.table_schema
                WHERE tc.table_schema = %s AND tc.table_name = %s
                  AND tc.constraint_type = 'PRIMARY KEY'
                """, (schema, table))
            return {r[0] for r in cur.fetchall()}

    def list_foreign_keys(self, schema):
        out = []
        with self.cursor() as cur:
            cur.execute(
                """
                SELECT kcu.table_name, kcu.column_name,
                       ccu.table_schema AS to_schema, ccu.table_name AS to_table,
                       ccu.column_name AS to_column
                FROM information_schema.key_column_usage kcu
                JOIN information_schema.referential_constraints rc
                    ON rc.constraint_name = kcu.constraint_name
                   AND rc.constraint_schema = kcu.constraint_schema
                JOIN information_schema.constraint_column_usage ccu
                    ON ccu.constraint_name = rc.unique_constraint_name
                   AND ccu.constraint_schema = rc.unique_constraint_schema
                WHERE kcu.table_schema = %s
                ORDER BY kcu.table_name, kcu.column_name
                """, (schema,))
            for from_table, from_col, to_schema, to_table, to_col in cur.fetchall():
                out.append({"from_table": from_table, "from_column": from_col,
                            "to_schema": to_schema, "to_table": to_table, "to_column": to_col})
        return out

    def _to_text(self, expr):
        return f"{expr}::text"

    def _numeric_cast(self, expr):
        return f"{expr}::numeric"


# ── MySQL ──────────────────────────────────────────────────────────────────────

class MySQLExtractor(Extractor):
    platform = "mysql"
    quote_char = "`"
    _SYSTEM = ("information_schema", "performance_schema", "mysql", "sys")

    def connect(self):
        import pymysql
        self._conn = pymysql.connect(
            host=self.env.get("host", "localhost"), port=int(self.env.get("port", 3306)),
            user=self.env.get("user", ""), password=self.env.get("password", ""),
            database=self.env.get("database", "") or None, connect_timeout=30)
        return self._conn

    def list_schemas(self):
        with self.cursor() as cur:
            cur.execute("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA ORDER BY SCHEMA_NAME")
            return [r[0] for r in cur.fetchall() if r[0].lower() not in self._SYSTEM]

    def list_relations(self, schema):
        out = []
        with self.cursor() as cur:
            cur.execute(
                """
                SELECT TABLE_NAME, TABLE_TYPE, TABLE_ROWS,
                       DATA_LENGTH + INDEX_LENGTH AS total_bytes, UPDATE_TIME, TABLE_COMMENT
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = %s AND TABLE_TYPE IN ('BASE TABLE','VIEW')
                ORDER BY TABLE_NAME
                """, (schema,))
            for name, ttype, row_est, total_bytes, update_time, comment in cur.fetchall():
                rc = int(row_est) if row_est is not None else None
                out.append({
                    "name": name, "relation_kind": "view" if ttype == "VIEW" else "table",
                    "row_count": rc, "row_count_is_estimate": rc is not None,
                    "size_bytes": int(total_bytes) if total_bytes is not None else None,
                    "last_modified": str(update_time) if update_time else None,
                    "num_files": None, "comment": comment or None,
                })
        return out

    def list_columns(self, schema, table):
        out = []
        with self.cursor() as cur:
            cur.execute(
                "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, ORDINAL_POSITION, "
                "COLUMN_COMMENT, COLUMN_KEY FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION",
                (schema, table))
            for name, dtype, nullable, ordinal, comment, ckey in cur.fetchall():
                out.append({"name": name, "data_type": dtype,
                            "nullable": nullable == "YES", "ordinal": ordinal,
                            "comment": comment or None, "primary_key": ckey == "PRI"})
        return out

    def list_foreign_keys(self, schema):
        out = []
        with self.cursor() as cur:
            cur.execute(
                """
                SELECT TABLE_NAME, COLUMN_NAME, REFERENCED_TABLE_SCHEMA,
                       REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME
                FROM information_schema.KEY_COLUMN_USAGE
                WHERE TABLE_SCHEMA = %s AND REFERENCED_TABLE_NAME IS NOT NULL
                ORDER BY TABLE_NAME, COLUMN_NAME
                """, (schema,))
            for from_table, from_col, to_schema, to_table, to_col in cur.fetchall():
                out.append({"from_table": from_table, "from_column": from_col,
                            "to_schema": to_schema, "to_table": to_table, "to_column": to_col})
        return out

    def _length_fn(self):
        return "CHAR_LENGTH"

    def _numeric_cast(self, expr):
        return f"CAST({expr} AS DECIMAL(38,6))"

    def _to_text(self, expr):
        return f"CAST({expr} AS CHAR)"


# ── Snowflake ──────────────────────────────────────────────────────────────────

class SnowflakeExtractor(Extractor):
    platform = "snowflake"
    quote_char = '"'
    _SYSTEM = ("information_schema", "account_usage")

    def connect(self):
        import snowflake.connector
        kwargs = {"account": self.env.get("account") or self.env.get("host", ""),
                  "user": self.env.get("user", ""), "password": self.env.get("password", ""),
                  "database": self.env.get("database", ""), "login_timeout": 30}
        for k in ("warehouse", "role", "schema"):
            if self.env.get(k):
                kwargs[k] = self.env[k]
        self._conn = snowflake.connector.connect(**kwargs)
        return self._conn

    def list_schemas(self):
        with self.cursor() as cur:
            cur.execute("SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA ORDER BY SCHEMA_NAME")
            return [r[0] for r in cur.fetchall() if r[0].lower() not in self._SYSTEM]

    def list_relations(self, schema):
        out = []
        with self.cursor() as cur:
            cur.execute(
                "SELECT TABLE_NAME, TABLE_TYPE, ROW_COUNT, BYTES, LAST_ALTERED, COMMENT "
                "FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = %s "
                "AND TABLE_TYPE IN ('BASE TABLE','VIEW') ORDER BY TABLE_NAME", (schema,))
            for name, ttype, rc, bytes_val, last_altered, comment in cur.fetchall():
                out.append({
                    "name": name, "relation_kind": "view" if ttype == "VIEW" else "table",
                    "row_count": int(rc) if rc is not None else None,
                    "row_count_is_estimate": rc is not None,
                    "size_bytes": int(bytes_val) if bytes_val is not None else None,
                    "last_modified": str(last_altered) if last_altered else None,
                    "num_files": None, "comment": comment or None,
                })
        return out

    def list_columns(self, schema, table):
        pks = self._primary_keys(schema, table)
        out = []
        with self.cursor() as cur:
            cur.execute(
                "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, ORDINAL_POSITION, COMMENT "
                "FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
                "ORDER BY ORDINAL_POSITION", (schema, table))
            for name, dtype, nullable, ordinal, comment in cur.fetchall():
                out.append({"name": name, "data_type": dtype,
                            "nullable": nullable == "YES", "ordinal": ordinal,
                            "comment": comment or None, "primary_key": name in pks})
        return out

    def _primary_keys(self, schema, table) -> set:
        try:
            with self.cursor() as cur:
                cur.execute(f'SHOW PRIMARY KEYS IN TABLE {self.qualified(schema, table)}')
                rows = cur.fetchall()
                cols = [d[0].lower() for d in cur.description]
                idx = cols.index("column_name") if "column_name" in cols else 4
                return {r[idx] for r in rows}
        except Exception:
            return set()

    def list_foreign_keys(self, schema):
        out = []
        try:
            with self.cursor() as cur:
                cur.execute(
                    """
                    SELECT fk.table_name, fk.column_name, pk.table_schema, pk.table_name, pk.column_name
                    FROM information_schema.referential_constraints rc
                    JOIN information_schema.key_column_usage fk
                        ON fk.constraint_name = rc.constraint_name
                       AND fk.constraint_schema = rc.constraint_schema
                    JOIN information_schema.key_column_usage pk
                        ON pk.constraint_name = rc.unique_constraint_name
                       AND pk.constraint_schema = rc.unique_constraint_schema
                       AND pk.ordinal_position = fk.position_in_unique_constraint
                    WHERE fk.table_schema = %s ORDER BY fk.table_name, fk.column_name
                    """, (schema,))
                for from_table, from_col, to_schema, to_table, to_col in cur.fetchall():
                    out.append({"from_table": from_table, "from_column": from_col,
                                "to_schema": to_schema, "to_table": to_table, "to_column": to_col})
        except Exception:
            self._rollback()
        return out

    def _numeric_cast(self, expr):
        return f"CAST({expr} AS DOUBLE)"

    def _to_text(self, expr):
        return f"CAST({expr} AS VARCHAR)"


# ── Databricks ─────────────────────────────────────────────────────────────────

class DatabricksExtractor(Extractor):
    platform = "databricks"
    quote_char = "`"
    _SYSTEM = ("information_schema",)

    def __init__(self, env):
        super().__init__(env)
        self.catalog = env.get("catalog", "")

    def connect(self):
        from databricks import sql as databricks_sql
        kwargs = {"server_hostname": self.env.get("host", ""),
                  "http_path": self.env.get("http_path", ""),
                  "access_token": self.env.get("token") or self.env.get("password", "")}
        if self.catalog:
            kwargs["catalog"] = self.catalog
        if self.env.get("schema"):
            kwargs["schema"] = self.env["schema"]
        self._conn = databricks_sql.connect(**kwargs)
        return self._conn

    def qualified(self, schema, table):
        if self.catalog:
            return f"{self.q(self.catalog)}.{self.q(schema)}.{self.q(table)}"
        return f"{self.q(schema)}.{self.q(table)}"

    def list_schemas(self):
        with self.cursor() as cur:
            if self.catalog:
                cur.execute(f"SHOW SCHEMAS IN {self.q(self.catalog)}")
            else:
                cur.execute("SHOW SCHEMAS")
            return [r[0] for r in cur.fetchall() if r[0].lower() not in self._SYSTEM]

    def list_relations(self, schema):
        out = []
        with self.cursor() as cur:
            scope = f"{self.q(self.catalog)}.{self.q(schema)}" if self.catalog else self.q(schema)
            cur.execute(f"SHOW TABLES IN {scope}")
            names = [r[1] for r in cur.fetchall()]  # (database, tableName, isTemporary)
            for name in names:
                size_bytes = num_files = last_modified = None
                comment = None
                try:
                    cur.execute(f"DESCRIBE DETAIL {self.qualified(schema, name)}")
                    row = cur.fetchone()
                    cols = [d[0].lower() for d in cur.description]
                    rec = dict(zip(cols, row))
                    size_bytes = _int(rec.get("sizeinbytes"))
                    num_files = _int(rec.get("numfiles"))
                    last_modified = str(rec.get("lastmodified")) if rec.get("lastmodified") else None
                except Exception:
                    pass
                out.append({"name": name, "relation_kind": "table", "row_count": None,
                            "row_count_is_estimate": False, "size_bytes": size_bytes,
                            "last_modified": last_modified, "num_files": num_files,
                            "comment": comment})
        return out

    def list_columns(self, schema, table):
        out = []
        with self.cursor() as cur:
            cur.execute(
                "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, ORDINAL_POSITION, COMMENT "
                "FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
                "ORDER BY ORDINAL_POSITION", (schema, table))
            for name, dtype, nullable, ordinal, comment in cur.fetchall():
                out.append({"name": name, "data_type": dtype,
                            "nullable": nullable == "YES", "ordinal": ordinal,
                            "comment": comment or None, "primary_key": False})
        return out

    def _length_fn(self):
        return "LENGTH"

    def _to_text(self, expr):
        return f"CAST({expr} AS STRING)"

    def _numeric_cast(self, expr):
        return f"CAST({expr} AS DOUBLE)"


_EXTRACTORS = {
    "postgres": PostgresExtractor, "postgresql": PostgresExtractor,
    "mysql": MySQLExtractor, "snowflake": SnowflakeExtractor,
    "databricks": DatabricksExtractor,
}


def get_extractor(platform: str, env: dict[str, str]) -> Extractor:
    cls = _EXTRACTORS.get((platform or "").lower())
    if cls is None:
        raise ValueError(f"unsupported platform {platform!r} "
                         f"(supported: {sorted(set(_EXTRACTORS))})")
    return cls(env)


def database_segment(platform: str, env: dict[str, str]) -> str:
    """The ``{db}`` estate-URI segment the manifest's ``catalog`` field carries so
    imported URIs MATCH a live scan byte-for-byte. Critically, this is NOT empty for
    2-level platforms: a live Postgres/MySQL scan uses the connected DATABASE name as
    the ``{db}`` segment (``estate_scan._identity_parts`` borrows ``connected_db``), so
    the extractor must record it. 3-level platforms use catalog/database."""
    p = (platform or "").lower()
    if p == "databricks":
        return env.get("catalog", "") or ""
    if p in ("postgres", "postgresql") and env.get("dsn") and not env.get("database"):
        from urllib.parse import urlparse
        return (urlparse(env["dsn"]).path or "/").lstrip("/")
    return env.get("database", "") or ""


# ── env → connection params (mirrors serving_runners:_conn_params_from_env) ────

def env_conn_params() -> tuple[str, dict[str, str]]:
    """Read WB_SOURCE_* → (platform, params). Credentials live ONLY here (env /
    .env), never in the manifest or the package."""
    g = os.environ.get
    platform = (g("WB_SOURCE_PLATFORM") or "postgres").lower()
    params = {
        "dsn": g("WB_SOURCE_DSN"), "host": g("WB_SOURCE_HOST"),
        "account": g("WB_SOURCE_ACCOUNT"), "port": g("WB_SOURCE_PORT"),
        "user": g("WB_SOURCE_USER"), "password": g("WB_SOURCE_PASSWORD"),
        "database": g("WB_SOURCE_DBNAME") or g("WB_SOURCE_DATABASE"),
        "schema": g("WB_SOURCE_SCHEMA"), "warehouse": g("WB_SOURCE_WAREHOUSE"),
        "role": g("WB_SOURCE_ROLE"), "http_path": g("WB_SOURCE_HTTP_PATH"),
        "catalog": g("WB_SOURCE_CATALOG"), "token": g("WB_SOURCE_TOKEN"),
    }
    return platform, {k: v for k, v in params.items() if v not in (None, "")}


# ── helpers ────────────────────────────────────────────────────────────────────

def _pg_dsn(env: dict[str, str]) -> str:
    if env.get("dsn"):
        return env["dsn"]
    from urllib.parse import quote
    user = env.get("user", "")
    pw = env.get("password", "")
    host = env.get("host", "localhost")
    port = env.get("port", 5432)
    db = env.get("database", "")
    up = f"{quote(str(user), safe='')}:{quote(str(pw), safe='')}@" if user else ""
    return f"postgresql://{up}{host}:{port}/{db}"


def _num(v):
    if v is None:
        return None
    try:
        from decimal import Decimal
        if isinstance(v, Decimal):
            return float(v)
        f = float(v)
        return round(f, 6) if f == f and f not in (float("inf"), float("-inf")) else None
    except (TypeError, ValueError):
        return None


def _int(v):
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _asdate(v):
    if v is None:
        return None
    try:
        return v.isoformat()
    except AttributeError:
        return str(v)
