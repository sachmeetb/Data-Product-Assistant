"""Sample-DB discovery, selection, load-token + provisioning-manifest builders.

The single source of truth tying together the things that must agree:
  * each sample's metadata (``samples/<name>/sample.json``): platform, target
    database, schemas — discovered at runtime, no hardcoded list,
  * what each sample's SQL under ``samples/<name>/`` actually creates,
  * the ``DWB_PG_*`` env the compose ``postgres`` service reads (user/password),
  * the load tokens handed to ``samples/_loader/{pg,mysql}-load.sh`` inside the
    containers (``name:db`` for Postgres, ``name:primary_schema`` for MySQL),
  * the quick-connect manifest the frontend prefills from.

Isolation model: **one database per sample** inside a single container per engine.
Postgres samples each get their own database (``hr`` / ``banking`` /
``products_sales``); MySQL samples each own their schema(s). Samples are loaded
idempotently on every ``dwb up`` by the loader scripts (existing sample skipped),
so ``--pg-sample all`` (the default) can load them all and a later single-sample
launch adds to the set without a volume wipe.

Backend-visible host/port differ by launch mode (see `provisioned_sources`):
  * compose → the service name on the compose network (`postgres:5432`, `mysql-hr:3306`)
  * host    → the published port on localhost (`localhost:5433`, `localhost:3307`)
"""
from __future__ import annotations

import json
from functools import lru_cache

from . import state

# Shared credentials for the CLI-managed Postgres sample container.
PG_USER = "workbench"
PG_PASSWORD = "workbenchpass"

# MySQL sample container root credentials (compose service `mysql-hr`).
MYSQL_USER = "root"
MYSQL_PASSWORD = "hrpass"

SAMPLES_DIR = state.ROOT / "samples"

# Sentinel selection meaning "every discovered sample for this platform".
ALL = "all"


# --- discovery ---------------------------------------------------------------
@lru_cache(maxsize=1)
def discover_samples() -> dict[str, dict]:
    """Read every ``samples/<name>/sample.json`` into a {name: manifest} map.

    Directories without a ``sample.json`` (e.g. ``intake/``, ``_loader/``) are
    skipped — presence of the manifest is what makes a directory a loadable DB
    sample. Bad JSON dies loudly so a malformed manifest can't silently drop a
    sample.
    """
    out: dict[str, dict] = {}
    if not SAMPLES_DIR.is_dir():
        return out
    for child in sorted(SAMPLES_DIR.iterdir()):
        manifest = child / "sample.json"
        if not manifest.is_file():
            continue
        try:
            data = json.loads(manifest.read_text())
        except (OSError, ValueError) as e:
            state.die(f"bad sample manifest {manifest.relative_to(state.ROOT)}: {e}")
        data.setdefault("name", child.name)
        _validate_manifest(data, manifest)
        out[data["name"]] = data
    return out


def _validate_manifest(data: dict, path) -> None:
    for key in ("platform", "database", "primary_schema"):
        if not data.get(key):
            state.die(f"sample manifest {path.relative_to(state.ROOT)} missing '{key}'")
    if data["platform"] not in ("postgres", "mysql"):
        state.die(f"sample manifest {path.relative_to(state.ROOT)}: unknown "
                  f"platform {data['platform']!r} (want postgres or mysql)")


def samples_for_platform(platform: str) -> list[dict]:
    return [m for m in discover_samples().values() if m["platform"] == platform]


def sample_names(platform: str) -> list[str]:
    return [m["name"] for m in samples_for_platform(platform)]


# --- selection + validation --------------------------------------------------
def resolve_selection(platform: str, requested: str | None) -> list[dict]:
    """Resolve a --pg-sample / --mysql-sample value into a list of manifests.

    ``requested`` is ``None`` / ``"all"`` (every sample for the platform), or a
    comma-separated list of sample names. A name belonging to the *other*
    platform, or an unknown name, dies with an actionable message.
    """
    available = {m["name"]: m for m in samples_for_platform(platform)}
    if requested in (None, "", ALL):
        return list(available.values())

    chosen: list[dict] = []
    seen: set[str] = set()
    for raw in requested.split(","):
        name = raw.strip()
        if not name:
            continue
        if name in available:
            if name not in seen:
                chosen.append(available[name])
                seen.add(name)
            continue
        # Not a sample for this platform — is it the other platform's?
        other = discover_samples().get(name)
        flag = "--pg-sample" if platform == "postgres" else "--mysql-sample"
        if other is not None:
            other_flag = "--mysql-sample" if other["platform"] == "mysql" else "--pg-sample"
            state.die(f"{flag}: {name!r} is a {other['platform']} sample — "
                      f"use {other_flag} instead.")
        state.die(f"{flag}: unknown sample {name!r} "
                  f"(available: {', '.join(available) or 'none'}).")
    return chosen


def load_tokens(platform: str, selected: list[dict]) -> list[str]:
    """Tokens for the in-container loader scripts.

    Postgres → ``name:database`` (loader createdb's each). MySQL → the SQL
    creates its own schemas, so the token carries ``name:primary_schema`` purely
    as the existence guard.
    """
    if platform == "postgres":
        return [f"{m['name']}:{m['database']}" for m in selected]
    return [f"{m['name']}:{m['primary_schema']}" for m in selected]


# --- compose env -------------------------------------------------------------
def pg_env() -> dict[str, str]:
    """DWB_PG_* passed to the compose ``postgres`` service.

    The service no longer bakes a single sample into its data dir — the loader
    creates per-sample databases post-start — so only the shared user/password
    matter here; POSTGRES_DB stays the maintenance ``postgres`` db (compose
    default in the override file).
    """
    return {
        "DWB_PG_USER": PG_USER,
        "DWB_PG_PASSWORD": PG_PASSWORD,
    }


# --- provisioning manifest ---------------------------------------------------
def provisioned_sources(mode: str, profiles: list[str],
                        pg_selected: list[dict],
                        mysql_selected: list[dict]) -> list[dict]:
    """Build the quick-connect manifest entries — one per loaded sample.

    host/port are **backend-visible** — the backend runs the live probe, so in
    compose mode it reaches a container by service name, in host mode by the
    published localhost port.
    """
    compose = mode == "compose"
    sources: list[dict] = []

    if "postgres" in profiles:
        for m in pg_selected:
            sources.append({
                "name": f"{m['name']}-postgres",
                "platform": "postgres",
                "host": state.SVC_POSTGRES if compose else "localhost",
                "port": 5432 if compose else state.PORT_PG,
                "database": m["database"],
                "username": PG_USER,
                "password": PG_PASSWORD,
                "schema": m["primary_schema"],
                "sample": m["name"],
                "mode": mode,
            })

    if "mysql" in profiles:
        for m in mysql_selected:
            sources.append({
                "name": m["name"],
                "platform": "mysql",
                "host": state.SVC_MYSQL if compose else "localhost",
                "port": 3306 if compose else state.PORT_MYSQL,
                "database": m["primary_schema"],
                "username": MYSQL_USER,
                "password": MYSQL_PASSWORD,
                "schema": m["primary_schema"],
                "sample": m["name"],
                "mode": mode,
            })

    if "storage" in profiles:
        # SeaweedFS S3-compatible object store started by `dwb up --with storage`.
        # Internal compose port is 8333; host-published port is PORT_S3 (9000).
        sources.append({
            "name": "seaweedfs",
            "platform": "s3",
            "host": state.SVC_SEAWEEDFS if compose else "localhost",
            "port": 8333 if compose else state.PORT_S3,
            "database": "data-workbench",
            "username": "workbench",
            "password": "workbenchsecret",
            "schema": "",
            "mode": mode,
        })

    return sources


# --- in-container loaders ----------------------------------------------------
def run_loaders(mode: str, profiles: list[str],
                pg_selected: list[dict], mysql_selected: list[dict]) -> None:
    """Load the selected samples into the running containers, idempotently.

    Runs ``samples/_loader/{pg,mysql}-load.sh`` inside the sample containers via
    ``docker compose exec``. The stack must already be up + healthy. Works in
    both launch modes — in host mode the sample containers are still
    compose-managed, so the exec path is identical.
    """
    from . import compose

    if "postgres" in profiles and pg_selected:
        tokens = load_tokens("postgres", pg_selected)
        state.info(f"Loading Postgres sample(s): {', '.join(t.split(':')[0] for t in tokens)}")
        compose.exec_svc(
            state.SVC_POSTGRES,
            ["bash", "/samples/_loader/pg-load.sh", *tokens],
            profiles=profiles, check=True,
        )

    if "mysql" in profiles and mysql_selected:
        tokens = load_tokens("mysql", mysql_selected)
        state.info(f"Loading MySQL sample(s): {', '.join(t.split(':')[0] for t in tokens)}")
        compose.exec_svc(
            state.SVC_MYSQL,
            ["bash", "/samples/_loader/mysql-load.sh", *tokens],
            profiles=profiles, check=True,
        )
