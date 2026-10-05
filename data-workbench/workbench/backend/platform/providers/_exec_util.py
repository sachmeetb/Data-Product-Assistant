"""Pure execution helpers shared by the per-platform QueryExecutor /
DeploymentProvider implementations and re-exported by the sql_executor facade.

No dependency on ``sql_executor`` (which imports the dispatcher, which imports
the providers) — keeping these here breaks that cycle. Pure functions only:
identifier rewriting, DBAPI type-name mapping, row serialisation.
"""
from __future__ import annotations

import json
import re
from typing import Any

DDL_TIMEOUT_MS = 60_000
SELECT_TIMEOUT_MS = 30_000


_MYSQL_VIEW_HEADER_RE = re.compile(
    r'^(CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+)'
    r'(?:"([^"]+)"|`([^`]+)`|([A-Za-z_][A-Za-z0-9_]*))\.'
    r'(?:"([^"]+)"|`([^`]+)`|([A-Za-z_][A-Za-z0-9_]*))',
    re.IGNORECASE | re.MULTILINE,
)


def rewrite_ddl_header_for_mysql(ddl: str) -> str:
    """Rewrite CREATE VIEW identifier quoting to MySQL backtick quoting.

    Handles ANSI double-quoted, backtick-quoted, AND unquoted schema.view names
    (generate_view_ddl emits unquoted names). Applied per-statement on the
    CREATE VIEW header line only.
    """
    def _rewrite(m: re.Match) -> str:
        prefix = m.group(1)
        schema = m.group(2) or m.group(3) or m.group(4) or ""
        view = m.group(5) or m.group(6) or m.group(7) or ""
        return f"{prefix}`{schema}`.`{view}`"
    return _MYSQL_VIEW_HEADER_RE.sub(_rewrite, ddl)


def generic_typename(type_code) -> str:
    """Fallback column-type resolver for non-Postgres platforms.

    DBAPI2 guarantees ``cursor.description`` but not a canonical type_code format —
    Snowflake and Databricks connectors return type-name strings; pymysql returns
    integer MySQL field-type codes. Return the string representation so the UI
    always has something to render.
    """
    if type_code is None:
        return "unknown"
    if isinstance(type_code, str):
        return type_code.lower()
    _MYSQL_TYPES = {
        0: "decimal", 1: "tinyint", 2: "smallint", 3: "int", 4: "float",
        5: "double", 7: "timestamp", 8: "bigint", 9: "mediumint", 10: "date",
        11: "time", 12: "datetime", 13: "year", 15: "varchar", 16: "bit",
        245: "json", 246: "decimal", 247: "enum", 248: "set",
        249: "tinyblob", 250: "mediumblob", 251: "longblob", 252: "text",
        253: "varchar", 254: "char", 255: "geometry",
    }
    try:
        return _MYSQL_TYPES.get(int(type_code), str(type_code))
    except (TypeError, ValueError):
        return str(type_code)


# Postgres OID → friendly type name. Small fixed map covers ~99% of column types
# we'll see in deployed views; unknowns fall back to the OID as a string.
_PG_TYPE_NAMES = {
    16: "boolean", 17: "bytea", 18: "char", 20: "bigint", 21: "smallint",
    23: "integer", 25: "text", 700: "real", 701: "double precision",
    1042: "char", 1043: "varchar", 1082: "date", 1083: "time",
    1114: "timestamp", 1184: "timestamptz", 1186: "interval",
    1700: "numeric", 2950: "uuid", 3802: "jsonb", 114: "json",
}


def pg_typname(oid) -> str:
    return _PG_TYPE_NAMES.get(oid, str(oid))


def serialize_row(row) -> list:
    """Convert a driver row tuple to a JSON-serializable list.

    Drivers return datetimes, Decimal, UUID, etc. natively; convert them to
    strings so the FastAPI JSON encoder doesn't choke."""
    out = []
    for v in row:
        if v is None:
            out.append(None)
        elif isinstance(v, (str, int, float, bool)):
            out.append(v)
        elif isinstance(v, (list, dict)):
            try:
                json.dumps(v)
                out.append(v)
            except (TypeError, ValueError):
                out.append(str(v))
        elif isinstance(v, bytes):
            out.append(v.decode("utf-8", errors="replace"))
        else:
            out.append(str(v))
    return out
