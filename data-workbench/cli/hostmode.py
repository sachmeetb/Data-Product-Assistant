"""Host launch mode — hot-reload backend + frontend as host processes, with
CLI-managed dependency containers.

Absorbs the old `./dev` logic: venv, uvicorn --reload, npm run dev, PID files in
.pids/, logs in .logs/. Dependencies follow the plan's detect-reuse-else-manage
rule: a compatible running Neo4j is reused (logged); otherwise the CLI compose-
manages one. Sample DBs + Gitea are ALWAYS compose-managed (same file/volumes as
compose mode) — only the app procs run on the host.
"""
from __future__ import annotations

import os
import signal
import subprocess
import urllib.error
import urllib.request

from . import compose, doctor, state

BACKEND_PID = state.PID_DIR / "backend.pid"
BACKEND_LOG = state.LOG_DIR / "backend.log"
FRONTEND_PID = state.PID_DIR / "frontend.pid"
FRONTEND_LOG = state.LOG_DIR / "frontend.log"


def _is_running(pidfile) -> bool:
    try:
        pid = int(pidfile.read_text().strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _neo4j_reachable() -> bool:
    try:
        with urllib.request.urlopen(state.NEO4J_REUSE_HTTP, timeout=2) as resp:
            return resp.status < 500
    except (urllib.error.URLError, OSError):
        return False


def _ensure_venv() -> bool:
    env_dir = state.ROOT / "env"
    if env_dir.is_dir():
        return True
    state.warn(f"No virtualenv at env/.")
    try:
        ans = input("  Create it now (python3 -m venv env + pip install)? [y/N] ").strip().lower()
    except EOFError:
        ans = "n"
    if ans not in ("y", "yes"):
        state.err("Host mode needs the venv — create it, then re-run:")
        print("    python3 -m venv env && env/bin/pip install -r workbench/backend/requirements.txt")
        return False
    state.info("Creating venv (this can take a few minutes)…")
    try:
        state.run(["python3", "-m", "venv", "env"])
        state.run(["env/bin/pip", "install", "-U", "pip"])
        state.run(["env/bin/pip", "install", "-r", "workbench/backend/requirements.txt"])
    except subprocess.CalledProcessError:
        state.err("venv creation failed — see output above.")
        return False
    state.ok("venv created")
    return True


def _backend_env(profiles: list[str] | None = None) -> dict[str, str]:
    """Merge repo-root .env so the host backend routes LLM calls correctly
    (the old ./dev relied on the ambient shell only).

    Observability: when the reference collector is enabled and the operator
    hasn't already pointed telemetry somewhere (shell env OR .env), default the
    OTLP endpoint at the collector's host-published :4318. Override-safe."""
    env = doctor._read_env_file()
    if profiles and "observability" in profiles:
        already = (os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
                   or env.get("OTEL_EXPORTER_OTLP_ENDPOINT") or "").strip()
        if not already:
            env["OTEL_EXPORTER_OTLP_ENDPOINT"] = f"http://localhost:{state.PORT_OTLP_HTTP}"
    return env


def _start_backend(profiles: list[str] | None = None) -> None:
    if _is_running(BACKEND_PID):
        state.ok(f"backend already running (pid {BACKEND_PID.read_text().strip()})")
        return
    state.info("Starting backend (uvicorn --reload)…")
    logf = open(BACKEND_LOG, "ab")
    proc = subprocess.Popen(
        ["env/bin/uvicorn", "workbench.backend.main:app",
         "--host", "0.0.0.0", "--port", str(state.PORT_BACKEND),
         "--reload", "--reload-exclude", "env/*", "--reload-exclude", "projects/*"],
        cwd=str(state.ROOT), stdout=logf, stderr=subprocess.STDOUT,
        env={**os.environ, **_backend_env(profiles)}, start_new_session=True,
    )
    BACKEND_PID.write_text(str(proc.pid))
    state.ok(f"backend started (pid {proc.pid}, http://localhost:{state.PORT_BACKEND})")


def _start_frontend() -> None:
    if _is_running(FRONTEND_PID):
        state.ok(f"frontend already running (pid {FRONTEND_PID.read_text().strip()})")
        return
    fe_dir = state.ROOT / "workbench" / "frontend"
    if not (fe_dir / "node_modules").is_dir():
        state.info("Installing frontend deps (npm install)…")
        state.run(["npm", "install"], cwd=fe_dir)
    state.info("Starting frontend (npm run dev)…")
    logf = open(FRONTEND_LOG, "ab")
    proc = subprocess.Popen(
        ["npm", "run", "dev", "--", "--port", str(state.PORT_FRONTEND)],
        cwd=str(fe_dir), stdout=logf, stderr=subprocess.STDOUT,
        env=os.environ.copy(), start_new_session=True,
    )
    FRONTEND_PID.write_text(str(proc.pid))
    state.ok(f"frontend started (pid {proc.pid}, http://localhost:{state.PORT_FRONTEND})")


def _stop_proc(pidfile, name: str) -> None:
    if not _is_running(pidfile):
        pidfile.unlink(missing_ok=True)
        state.ok(f"{name} not running")
        return
    pid = int(pidfile.read_text().strip())
    # Kill the whole process group — uvicorn --reload and vite spawn children.
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    pidfile.unlink(missing_ok=True)
    state.ok(f"{name} stopped (pid {pid})")


def _infra_services(profiles: list[str], manage_neo4j: bool) -> list[str]:
    services: list[str] = []
    if manage_neo4j:
        services.append(state.SVC_NEO4J)
    if "postgres" in profiles:
        services.append(state.SVC_POSTGRES)
    if "mysql" in profiles:
        services.append(state.SVC_MYSQL)
    if "gitea" in profiles:
        services.append(state.SVC_GITEA)
    if "storage" in profiles:
        services.append(state.SVC_SEAWEEDFS)
    return services


def up(profiles: list[str]) -> bool:
    # Neo4j: detect-reuse-else-manage.
    manage_neo4j = not _neo4j_reachable()
    if manage_neo4j:
        state.info(f"No Neo4j at {state.NEO4J_REUSE_HTTP} — compose-managing one "
                   f"(bolt localhost:{state.PORT_NEO4J_BOLT}). "
                   f"Point Settings → Neo4j there if this is a fresh graph.")
    else:
        state.ok(f"Reusing the Neo4j reachable at {state.NEO4J_REUSE_HTTP}")

    infra = _infra_services(profiles, manage_neo4j)
    if infra:
        state.info(f"Starting dependency containers: {', '.join(infra)}")
        compose.up(profiles, build=False, services=infra, wait=True)

    if not _ensure_venv():
        return False

    _start_backend(profiles)
    _start_frontend()
    return True


def tail_logs() -> None:
    state.info("Tailing app logs (Ctrl-C to detach; procs keep running)…")
    try:
        state.run(["tail", "-f", str(BACKEND_LOG), str(FRONTEND_LOG)], check=False)
    except KeyboardInterrupt:
        pass


def down(profiles: list[str], *, volumes: bool = False) -> None:
    _stop_proc(BACKEND_PID, "backend")
    _stop_proc(FRONTEND_PID, "frontend")
    # Also tear down the CLI-managed dependency containers. A REUSED external
    # Neo4j lives in a different compose project, so `down` never touches it.
    compose.down(profiles, volumes=volumes)


def restart(target: str) -> None:
    profiles = state.load_state().get("profiles", [])
    if target in ("backend", "all"):
        _stop_proc(BACKEND_PID, "backend")
    if target in ("frontend", "all"):
        _stop_proc(FRONTEND_PID, "frontend")
    if target in ("backend", "all"):
        _start_backend(profiles)
    if target in ("frontend", "all"):
        _start_frontend()


def proc_status() -> list[tuple[str, bool, str]]:
    rows = []
    for name, pidfile, url in (
        ("backend", BACKEND_PID, f"http://localhost:{state.PORT_BACKEND}"),
        ("frontend", FRONTEND_PID, f"http://localhost:{state.PORT_FRONTEND}"),
    ):
        running = _is_running(pidfile)
        pid = pidfile.read_text().strip() if running else "—"
        rows.append((name, running, f"pid={pid}  {url}"))
    return rows
