# DLT destinations — native vs SQLAlchemy

Pick the target's `dlt[<extra>]` requirement and destination factory:

| Target | dlt extra | Destination | Notes |
|---|---|---|---|
| Postgres | `dlt[postgres]` | `dlt.destinations.postgres(credentials=dsn)` | native |
| Snowflake | `dlt[snowflake]` | `dlt.destinations.snowflake(credentials={...})` | native; needs account + warehouse |
| Databricks | `dlt[databricks]` | `dlt.destinations.databricks(credentials={...})` | native; needs http_path + token + catalog |
| Redshift | `dlt[redshift]` | `dlt.destinations.redshift(...)` | native (Postgres-wire) |
| BigQuery | `dlt[bigquery]` | `dlt.destinations.bigquery(...)` | native; service-account creds |
| MySQL | `dlt[sqlalchemy]` | `dlt.destinations.sqlalchemy(credentials=url)` | via SQLAlchemy dest |
| Oracle / DB2 | `dlt[sqlalchemy]` | `dlt.destinations.sqlalchemy(credentials=url)` | via SQLAlchemy dest |

The migration runner reads target credentials from `WB_TARGET_*` env
(`build_runner_env` maps them; secrets ride the subprocess env only). The
destination-config surface for warehouse targets is **experimental** — per-
deployment tuning of account/warehouse/role (Snowflake) or http_path/catalog
(Databricks) may be needed.

`dataset_name` on the dlt pipeline is the **target schema** the tables land in.
