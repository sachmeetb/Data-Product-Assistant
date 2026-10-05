"""Docker Compose driver — profile flags, sample-DB env passthrough, and the
compose-mode up/down/logs/status primitives. The CLI never edits the compose
files; it passes `--profile` flags and `DWB_PG_*` env through the subprocess.
"""
from __future__ import annotations

import json
import os

from . import samples, state


def base_cmd(profiles: list[str] | None = None) -> list[str]:
    cmd = ["docker", "compose"]
    for p in profiles or []:
        # gitea/postgres/mysql/storage/observability are the profile names; only
        # pass those.
        if p in ("postgres", "mysql", "gitea", "storage", "observability"):
            cmd += ["--profile", p]
    return cmd


def compose_env(profiles: list[str] | None = None) -> dict[str, str]:
    return {**samples.pg_env(), **_otel_compose_env(profiles or [])}


def _otel_compose_env(profiles: list[str]) -> dict[str, str]:
    """When the reference collector is enabled and the operator hasn't already
    pointed telemetry somewhere (shell env OR repo .env), default the OTLP
    endpoint at the in-network collector so `${OTEL_EXPORTER_OTLP_ENDPOINT:-}`
    in the compose backend env resolves to it. Override-safe: a vendor endpoint
    the user set always wins (we return {} and let compose substitution pick it)."""
    if "observability" not in profiles:
        return {}
    from . import doctor  # local import avoids any module-load cycle
    already = (os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
               or doctor._read_env_file().get("OTEL_EXPORTER_OTLP_ENDPOINT")
               or "").strip()
    if already:
        return {}
    return {"OTEL_EXPORTER_OTLP_ENDPOINT":
            f"http://{state.SVC_OTEL_COLLECTOR}:{state.PORT_OTLP_HTTP}"}


def up(profiles: list[str], *, build: bool = True,
       foreground: bool = False, services: list[str] | None = None,
       wait: bool = True) -> None:
    cmd = base_cmd(profiles) + ["up"]
    if build:
        cmd.append("--build")
    if foreground:
        # attach — blocks; healthcheck polling is moot.
        pass
    else:
        cmd.append("-d")
        if wait:
            cmd.append("--wait")
    if services:
        cmd += services
    state.run(cmd, env=compose_env(profiles))


def down(profiles: list[str], *, volumes: bool = False) -> None:
    cmd = base_cmd(profiles) + ["down"]
    if volumes:
        cmd.append("-v")
    # down doesn't consume DWB_PG_* but pass them so volume/profile resolution
    # is identical to `up`.
    state.run(cmd, check=False)


def logs(profiles: list[str], service: str | None) -> None:
    cmd = base_cmd(profiles) + ["logs", "-f", "--tail", "200"]
    if service:
        cmd.append(service)
    state.run(cmd, check=False)


def exec_svc(service: str, args: list[str], *, profiles: list[str] | None = None,
             env: dict | None = None, user: str | None = None,
             capture: bool = False, check: bool = True):
    # Carry the profile flags so a profiled service (e.g. gitea) is resolvable.
    cmd = base_cmd(profiles) + ["exec"]
    if user:
        cmd += ["-u", user]
    for k, v in (env or {}).items():
        cmd += ["-e", f"{k}={v}"]
    cmd += ["-T", service] + args
    return state.run(cmd, capture=capture, check=check, quiet=capture)


def ps(profiles: list[str]) -> list[dict]:
    """Return parsed `docker compose ps` rows (best-effort)."""
    cmd = base_cmd(profiles) + ["ps", "--format", "json"]
    try:
        res = state.run(cmd, capture=True, check=True)
    except Exception:
        return []
    out = (res.stdout or "").strip()
    if not out:
        return []
    # compose v2 emits either NDJSON (one object per line) or a JSON array.
    rows: list[dict] = []
    if out.startswith("["):
        try:
            rows = json.loads(out)
        except ValueError:
            rows = []
    else:
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows
