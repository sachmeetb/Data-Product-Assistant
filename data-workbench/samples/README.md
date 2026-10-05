# Sample databases

Each subdirectory here is a self-describing demo dataset the `dwb` launcher can
load into a CLI-managed Postgres or MySQL container. A directory becomes a
loadable sample by containing a **`sample.json`** manifest; directories without
one (e.g. `intake/`, `_loader/`) are ignored by the launcher.

## Anatomy of a sample

```
samples/<name>/
├── sample.json          # manifest (required — makes it discoverable)
├── 01_schema.sql        # DDL — schemas + tables
├── 02_seed.sql          # seed data
└── 03_consumer_schema.sql   # optional downstream/consumer objects
```

SQL files load in lexical order (`01_ → 02_ → 03_`).

### `sample.json`

```json
{
  "name": "products_sales",
  "platform": "postgres",
  "domain": "Retail / E-commerce",
  "description": "Product catalogue, orders, customers, and sales transactions.",
  "database": "products_sales",
  "schemas": ["products_sales", "wb_views"],
  "primary_schema": "products_sales"
}
```

| field | meaning |
|---|---|
| `name` | CLI reference + connection-name stem (defaults to the directory name) |
| `platform` | `postgres` or `mysql` — validated; a sample passed to the wrong `--*-sample` flag is rejected |
| `domain` / `description` | display metadata |
| `database` | **Postgres:** the database the loader `createdb`s and loads this sample into (one DB per sample). **MySQL:** the primary schema/db the SQL creates |
| `schemas` | all schemas the SQL creates (informational) |
| `primary_schema` | prefilled default schema for the quick-connect entry + the loader's idempotency existence-guard key |

## How loading works

`dwb up` brings the container up, then runs `_loader/pg-load.sh` /
`_loader/mysql-load.sh` **inside** the container (the whole `samples/` tree is
mounted read-only at `/samples`). The loaders are **idempotent** — a sample whose
database (Postgres) or primary schema (MySQL) already exists is skipped, so
re-running `dwb up` is safe and new samples can be added without a volume wipe.

- `dwb up --with postgres` → loads **all** Postgres samples, each into its own DB.
- `dwb up --pg-sample banking` → loads just `banking` (auto-enables the postgres
  profile); other samples are left untouched.
- `dwb up --with mysql` / `--mysql-sample <name>` → same for MySQL.

Each loaded sample is registered as a separate **Quick connect** entry
(`<name>-postgres` / `<name>`) in `.dwb/provisioned-sources.json`.

## Adding a new sample

1. Create `samples/<name>/` with the SQL files and a `sample.json`.
2. For Postgres, keep schema/DB names distinct from other samples (isolation is
   at the database level, so collisions are impossible, but distinct names keep
   the UI legible).
3. That's it — the launcher discovers it automatically; no CLI code changes.

## Cross-engine note

`hr` (Postgres) and `hr-mysql` (MySQL) model the same HR domain but are
hand-maintained per dialect (`BIGSERIAL` vs `AUTO_INCREMENT`/`ENGINE=InnoDB`).
They are independent samples that happen to share a `domain`.
