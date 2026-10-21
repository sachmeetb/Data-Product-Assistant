"""`dwb` command-line entry point.

    dwb up [--mode host|compose] [--with postgres,mysql]
           [--pg-sample all|<name>[,<name>]] [--mysql-sample all|<name>[,<name>]]
           [--gitea|--no-gitea] [--foreground]
    dwb down [--volumes]
    dwb status
    dwb doctor
    dwb reset
    dwb logs [backend|frontend|neo4j|postgres|mysql|gitea|all]
    dwb connect
    dwb restart [backend|frontend|all]

Sample DBs are discovered from `samples/<name>/sample.json`. Naming a platform
with `--with` (and no explicit sample flag) loads *all* of that platform's
samples; naming a sample flag auto-enables its platform. Each sample lands in its
own database (Postgres) / schema (MySQL) inside a single per-engine container.
"""
from __future__ import annotations

import argparse
import sys
from functools import partial

from . import compose, connect, doctor, gitea, hostmode, samples, state


# --- profile resolution ------------------------------------------------------
def _resolve_profiles(with_arg: str, pg_requested: str | None,
                      mysql_requested: str | None, gitea_on: bool,
                      storage_on: bool = False,
                      observability_on: bool = False) -> list[str]:
    """Build the compose profile list from --with plus the sample/infra flags.

    An explicit --pg-sample / --mysql-sample auto-enables its platform even when
    it wasn't named in --with ("pick a sample → get the right container"). The
    object store is infra (like gitea): enable it with --storage OR by naming
    `storage` in --with (alias `objectstore`). The reference OTel collector is
    infra too: --observability OR `observability` in --with (aliases `otel`,
    `telemetry`).
    """
    profiles: list[str] = []
    for item in (with_arg or "").split(","):
        item = item.strip().lower()
        if not item:
            continue
        if item in ("storage", "objectstore"):
            item = "storage"
        elif item in ("observability", "otel", "telemetry"):
            item = "observability"
        elif item not in ("postgres", "mysql"):
            state.die(f"--with: unknown item {item!r} "
                      "(choose postgres, mysql, storage, and/or observability).")
        if item not in profiles:
            profiles.append(item)
    if pg_requested is not None and "postgres" not in profiles:
        profiles.append("postgres")
    if mysql_requested is not None and "mysql" not in profiles:
        profiles.append("mysql")
    if gitea_on and "gitea" not in profiles:
        profiles.append("gitea")
    if storage_on and "storage" not in profiles:
        profiles.append("storage")
    if observability_on and "observability" not in profiles:
        profiles.append("observability")
    return profiles


# --- up ----------------------------------------------------------------------
def do_up(mode: str, profiles: list[str], pg_selected: list[dict],
          mysql_selected: list[dict], foreground: bool) -> int:
    state.ensure_dirs()

    if not doctor.preflight(mode, profiles):
        return 1

    # Write the quick-connect manifest + persist state BEFORE bringing the stack
    # up so the backend's read-only .dwb mount sees the file at boot.
    connect.write_manifest(mode, profiles, pg_selected, mysql_selected)
    state.save_state(mode, profiles,
                     [m["name"] for m in pg_selected],
                     [m["name"] for m in mysql_selected])

    def _names(sel: list[dict]) -> str:
        return ", ".join(m["name"] for m in sel) or "none"

    print()
    launch = (f"Launching in {state.GREEN}{mode}{state.NC} mode "
              f"(profiles: {', '.join(profiles) or 'core only'}")
    if "postgres" in profiles:
        launch += f", pg: {_names(pg_selected)}"
    if "mysql" in profiles:
        launch += f", mysql: {_names(mysql_selected)}"
    state.info(launch + ")")

    if mode == "compose":
        compose.up(profiles, build=True, foreground=False, wait=True)
    else:
        if not hostmode.up(profiles):
            return 1

    # Stack is up + healthy — load the selected samples into the containers
    # (idempotent: existing databases/schemas are skipped).
    samples.run_loaders(mode, profiles, pg_selected, mysql_selected)

    healthy = doctor.wait_for_app()

    if "gitea" in profiles and healthy:
        gitea.bootstrap(mode)

    _print_up_summary(mode, profiles, pg_selected, mysql_selected)

    if foreground:
        if mode == "compose":
            compose.logs(profiles, None)
        else:
            hostmode.tail_logs()
    return 0


def _print_up_summary(mode: str, profiles: list[str],
                      pg_selected: list[dict], mysql_selected: list[dict]) -> None:
    print()
    print(f"{state.GREEN}Data Workbench is up.{state.NC}")
    print(f"  UI       http://localhost:{state.PORT_FRONTEND}")
    print(f"  API      http://localhost:{state.PORT_BACKEND}/api/health")
    if mode == "compose":
        print(f"  Neo4j    http://localhost:{state.PORT_NEO4J_HTTP}  (bolt {state.PORT_NEO4J_BOLT})")
    if "postgres" in profiles:
        print(f"  Postgres localhost:{state.PORT_PG}  user={samples.PG_USER}")
        for m in pg_selected:
            print(f"    · {m['name']:<16} db={m['database']}  (connect: {m['name']}-postgres)")
    if "mysql" in profiles:
        print(f"  MySQL    localhost:{state.PORT_MYSQL}  user={samples.MYSQL_USER}")
        for m in mysql_selected:
            print(f"    · {m['name']:<16} schema={m['primary_schema']}  (connect: {m['name']})")
    if "gitea" in profiles:
        print(f"  Gitea    http://localhost:{state.PORT_GITEA}  ({gitea.GIT_USER}/{gitea.GIT_PASSWORD})")
    if "storage" in profiles:
        print(f"  ObjStore S3 http://localhost:{state.PORT_S3}  "
              f"(key workbench/workbenchsecret) · browse http://localhost:{state.PORT_S3_CONSOLE}")
    if "observability" in profiles:
        print(f"  Telemetry Grafana http://localhost:{state.PORT_GRAFANA}  "
              f"· OTLP http://localhost:{state.PORT_OTLP_HTTP}")
    if "postgres" in profiles or "mysql" in profiles:
        print(f"\n  {state.DIM}Sample DBs appear as a “Quick connect” prefill in the "
              f"connection forms.{state.NC}")
    print(f"  Logs: dwb logs [backend|frontend|all]   Stop: dwb down")


# --- down --------------------------------------------------------------------
def do_down(volumes: bool) -> int:
    st = state.load_state()
    mode = st.get("mode", "compose")
    profiles = st.get("profiles", ["gitea"])
    state.info(f"Stopping ({mode} mode)" + (" and removing volumes" if volumes else ""))
    if mode == "host":
        hostmode.down(profiles, volumes=volumes)
    else:
        compose.down(profiles, volumes=volumes)
    if volumes:
        connect.clear_manifest()
    state.ok("Down.")
    return 0


# --- status ------------------------------------------------------------------
def do_status() -> int:
    st = state.load_state()
    if not st:
        state.warn("No recorded launch — run `dwb up`.")
        return 0
    mode = st.get("mode", "compose")
    profiles = st.get("profiles", [])
    print(f"\n{state.BLUE}Data Workbench{state.NC}  (mode: {mode}, started: "
          f"{st.get('started_at', '?')})")
    print("─" * 48)

    if mode == "host":
        for name, running, detail in hostmode.proc_status():
            mark = f"{state.GREEN}running{state.NC}" if running else f"{state.RED}stopped{state.NC}"
            print(f"  {name:<10} {mark}  {detail}")
        neo = hostmode._neo4j_reachable()
        print(f"  {'neo4j':<10} " + (f"{state.GREEN}reachable{state.NC}  {state.NEO4J_REUSE_HTTP}"
              if neo else f"{state.YELLOW}not reachable{state.NC}  {state.NEO4J_REUSE_HTTP}"))

    rows = compose.ps(profiles)
    if rows:
        print(f"  {state.DIM}containers:{state.NC}")
        for r in rows:
            svc = r.get("Service") or r.get("Name", "?")
            st_ = r.get("State", "?")
            health = r.get("Health", "")
            hz = f"  ({health})" if health else ""
            colour = state.GREEN if st_ == "running" else state.YELLOW
            print(f"    {svc:<12} {colour}{st_}{state.NC}{hz}")
    elif mode == "compose":
        print(f"  {state.YELLOW}no containers running{state.NC}")
    print()
    return 0


# --- logs --------------------------------------------------------------------
_LOG_SVC = {"postgres": state.SVC_POSTGRES, "mysql": state.SVC_MYSQL,
            "neo4j": state.SVC_NEO4J, "gitea": state.SVC_GITEA,
            "seaweedfs": state.SVC_SEAWEEDFS,
            "observability": state.SVC_OTEL_COLLECTOR,
            "backend": state.SVC_BACKEND, "frontend": state.SVC_FRONTEND}


def do_logs(target: str) -> int:
    st = state.load_state()
    mode = st.get("mode", "compose")
    profiles = st.get("profiles", [])
    if mode == "host" and target in ("backend", "frontend", "all"):
        logf = {"backend": hostmode.BACKEND_LOG, "frontend": hostmode.FRONTEND_LOG}
        files = [str(logf[target])] if target in logf else [str(hostmode.BACKEND_LOG), str(hostmode.FRONTEND_LOG)]
        state.run(["tail", "-f", *files], check=False)
        return 0
    # compose-managed (or a dependency container in host mode)
    svc = None if target == "all" else _LOG_SVC.get(target)
    if target != "all" and svc is None:
        state.die(f"unknown logs target {target!r}")
    compose.logs(profiles, svc)
    return 0


# --- restart -----------------------------------------------------------------
def do_restart(target: str) -> int:
    st = state.load_state()
    mode = st.get("mode", "compose")
    profiles = st.get("profiles", [])
    if mode == "host":
        hostmode.restart(target)
    else:
        svcs = [state.SVC_BACKEND, state.SVC_FRONTEND] if target == "all" else [_LOG_SVC.get(target, target)]
        state.run(compose.base_cmd(profiles) + ["restart", *svcs], check=False)
    state.ok(f"Restarted {target}.")
    return 0


# --- argparse ----------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dwb", description="Data Workbench launcher.")
    sub = p.add_subparsers(dest="cmd", required=True)

    up = sub.add_parser("up", help="launch the stack")
    up.add_argument("--mode", choices=["host", "compose"], default="compose")
    up.add_argument("--with", dest="with_", default="",
                    help="comma list of opt-in services: postgres,mysql,storage,observability")
    up.add_argument("--pg-sample", default=None,
                    help="Postgres sample(s): 'all' or comma list of names "
                         f"({', '.join(samples.sample_names('postgres')) or 'none'}); "
                         "default: all when postgres is enabled")
    up.add_argument("--mysql-sample", default=None,
                    help="MySQL sample(s): 'all' or comma list of names "
                         f"({', '.join(samples.sample_names('mysql')) or 'none'}); "
                         "default: all when mysql is enabled")
    up.add_argument("--gitea", action=argparse.BooleanOptionalAction, default=True,
                    help="bring up + bootstrap Gitea (default on)")
    up.add_argument("--storage", action=argparse.BooleanOptionalAction, default=False,
                    help="bring up the S3-compatible object store (SeaweedFS) for "
                         "publishing data artifacts (default off; same as --with storage)")
    up.add_argument("--observability", action=argparse.BooleanOptionalAction, default=False,
                    help="bring up the reference OpenTelemetry collector (Grafana "
                         "otel-lgtm) + wire telemetry to it (default off; same as "
                         "--with observability)")
    up.add_argument("--foreground", action="store_true", help="tail logs after launch")

    down = sub.add_parser("down", help="graceful stop")
    down.add_argument("--volumes", action="store_true", help="also remove named volumes")

    sub.add_parser("status", help="app + dependency health")
    sub.add_parser("doctor", help="preflight checks")
    sub.add_parser("reset", help="interactive soft/hard reset")
    sub.add_parser("connect", help="(re)write the quick-connect manifest")

    logs = sub.add_parser("logs", help="tail logs")
    logs.add_argument("target", nargs="?", default="all",
                      choices=["backend", "frontend", "neo4j", "postgres", "mysql",
                               "gitea", "seaweedfs", "observability", "all"])

    restart = sub.add_parser("restart", help="restart app procs")
    restart.add_argument("target", nargs="?", default="all",
                         choices=["backend", "frontend", "all"])

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.cmd == "up":
        profiles = _resolve_profiles(args.with_, args.pg_sample, args.mysql_sample,
                                     args.gitea, args.storage, args.observability)
        pg_selected = samples.resolve_selection("postgres", args.pg_sample) if "postgres" in profiles else []
        mysql_selected = samples.resolve_selection("mysql", args.mysql_sample) if "mysql" in profiles else []
        return do_up(args.mode, profiles, pg_selected, mysql_selected, args.foreground)
    if args.cmd == "down":
        return do_down(args.volumes)
    if args.cmd == "status":
        return do_status()
    if args.cmd == "doctor":
        return doctor.cmd_doctor()
    if args.cmd == "connect":
        return connect.cmd_connect()
    if args.cmd == "logs":
        return do_logs(args.target)
    if args.cmd == "restart":
        return do_restart(args.target)
    if args.cmd == "reset":
        from . import reset
        st = state.load_state()
        mode = st.get("mode", "compose")
        profiles = st.get("profiles", ["gitea"])
        pg_selected = samples.resolve_selection(
            "postgres", ",".join(state.state_samples(st, "postgres")) or None) \
            if "postgres" in profiles else []
        mysql_selected = samples.resolve_selection(
            "mysql", ",".join(state.state_samples(st, "mysql")) or None) \
            if "mysql" in profiles else []
        reup = partial(do_up, mode, profiles, pg_selected, mysql_selected, False)
        return reset.cmd_reset(reup)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        sys.exit(130)
