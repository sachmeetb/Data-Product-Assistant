# DLT source — `sql_database` (per SQLAlchemy dialect)

DLT reads relational sources through the `sql_database` verified source, which uses
**SQLAlchemy** under the hood. Any engine with a SQLAlchemy dialect is reachable:
Postgres, MySQL, MS SQL / Azure SQL, Oracle, IBM DB2, Teradata, Snowflake, Redshift.

Connection is a SQLAlchemy URL:

- Postgres: `postgresql+psycopg2://user:pw@host:5432/db`  (driver: `psycopg2-binary`)
- MySQL:    `mysql+pymysql://user:pw@host:3306/db`         (driver: `pymysql`)

Per-table extraction (what the migration runner uses):

```python
from dlt.sources.sql_database import sql_table
resource = sql_table(credentials=engine, schema="public", table="employees",
                     write_disposition="replace")
```

Backends: `sqlalchemy` (default, portable), `pyarrow`/`connectorx` (faster, needs
extra deps). The reference runner uses the default SQLAlchemy backend for maximum
dialect coverage.

Reflection notes: `sql_database` reflects columns + types from the source. Very
wide tables or exotic column types can slow reflection; a table with no primary
key cannot use `merge` (use `replace`/`append`).
