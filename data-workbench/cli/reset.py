"""Interactive environment reset — soft (keep data) vs hard (nuke + re-up).

Soft:  run scripts/reset_demo_env.py (blank graph + SQLite rows, source tables
       kept), clear projects/ on disk, and delete the Gitea org's product repos.
Hard:  `docker compose down -v` (drop every named volume), also delete
       workbench.db + projects/ on disk in host mode, then re-`up` and re-bootstrap
       Gitea — a truly blank graph + SQLite + projects + Git server.
"""
from __future__ import annotations

import shutil
from typing import Callable

from . import compose, gitea, hostmode, state


def _clear_projects(mode: str, profiles: list[str]) -> None:
    if mode == "compose":
        # The container's per-project scratch lives in the wb_projects volume at
        # /app/projects — clearing the host's projects/ dir does nothing for it.
        compose.exec_svc(
            state.SVC_BACKEND,
            ["sh", "-c", "rm -rf /app/projects/* /app/projects/.[!.]* 2>/dev/null || true"],
            profiles=profiles, check=False,
        )
        state.ok("Cleared container /app/projects scratch")
        return
    proj = state.ROOT / "projects"
    if not proj.is_dir():
        return
    n = 0
    for child in proj.iterdir():
        try:
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
            n += 1
        except OSError:
            pass
    state.ok(f"Cleared projects/ ({n} entr{'y' if n == 1 else 'ies'} removed)")


def _reset_env_vars(mode: str) -> dict[str, str]:
    env = {"WB_ALLOW_DEMO_RESET": "1", "WB_DEMO_RESET_OK": "1"}
    if mode == "compose":
        # The reset script imports `workbench.*`; the container's WORKDIR is /app
        # but the script's own dir shadows it on sys.path, so set PYTHONPATH.
        env["PYTHONPATH"] = "/app"
    else:
        env["PYTHONPATH"] = str(state.ROOT)
        # Host backend reaches Neo4j on localhost; pick the port by whether we
        # reused an external instance (7687) or compose-manage our own (7688).
        env["WB_NEO4J_HOST"] = "localhost"
        env["WB_NEO4J_PORT"] = "7687" if hostmode._neo4j_reachable() else str(state.PORT_NEO4J_BOLT)
    return env


def _run_soft_wipe(mode: str, profiles: list[str]) -> bool:
    state.info("Running clean-slate wipe (graph + SQLite; source tables kept)…")
    envs = _reset_env_vars(mode)
    if mode == "compose":
        res = compose.exec_svc(state.SVC_BACKEND,
                               ["python", "scripts/reset_demo_env.py", "--yes"],
                               profiles=profiles, env=envs, check=False)
    else:
        res = state.run(["env/bin/python", "scripts/reset_demo_env.py", "--yes"],
                        env=envs, check=False)
    if res.returncode != 0:
        state.err(f"wipe script failed (exit {res.returncode}) — graph/SQLite were "
                  f"NOT cleared. See the output above.")
        return False
    return True


def cmd_reset(reup: Callable[[], int]) -> int:
    st = state.load_state()
    if not st:
        state.warn("No recorded launch (.dwb/state.json missing) — assuming compose "
                   "mode with the gitea profile.")
    mode = st.get("mode", "compose")
    profiles = st.get("profiles", ["gitea"])

    print(f"\n{state.BLUE}Reset Data Workbench{state.NC}  (mode: {mode}, profiles: "
          f"{', '.join(profiles) or 'core only'})")
    print("  soft  — blank the graph + app DB + projects + Git repos; keep sample DB volumes")
    print("  hard  — down -v (nuke ALL volumes) + re-up + re-bootstrap Gitea")
    try:
        choice = input(f"  Choose [{state.GREEN}soft{state.NC}/hard/cancel]: ").strip().lower()
    except EOFError:
        choice = "cancel"

    if choice in ("cancel", "c", ""):
        state.info("Cancelled.")
        return 0

    if choice in ("soft", "s"):
        wiped = _run_soft_wipe(mode, profiles)
        _clear_projects(mode, profiles)
        if "gitea" in profiles:
            gitea.clear_org_repos()
        if wiped:
            state.info("Soft reset complete — blank graph, no projects, sample data intact.")
            return 0
        state.err("Soft reset INCOMPLETE — the graph/SQLite wipe failed (see above).")
        return 1

    if choice in ("hard", "h"):
        state.info("Hard reset — tearing down volumes…")
        # `down -v` only removes volumes for services in the ACTIVE profiles, so
        # a teardown scoped to the recorded profiles leaves the profile-gated
        # volumes (postgres_data / mysql_hr_data / gitea_data / wb_object_store)
        # behind when they weren't recorded — a stale sample DB or object store
        # then survives the "nuke everything" reset. Force the full profile set so
        # hard truly means hard.
        down_profiles = list(dict.fromkeys([*profiles, "postgres", "mysql", "gitea", "storage"]))
        compose.down(down_profiles, volumes=True)
        if mode == "host":
            hostmode.down(profiles, volumes=True)  # stop app procs too
            db = state.ROOT / "workbench.db"
            if db.exists():
                db.unlink()
                state.ok("Deleted workbench.db")
            _clear_projects(mode, profiles)
        state.info("Re-launching…")
        rc = reup()
        if rc != 0:
            state.err("Re-launch failed after hard reset — see output above.")
            return rc
        if "gitea" in profiles:
            gitea.bootstrap(mode)
        state.info("Hard reset complete — everything blank, Gitea re-bootstrapped.")
        return 0

    state.warn(f"Unrecognised choice {choice!r} — nothing done.")
    return 1
