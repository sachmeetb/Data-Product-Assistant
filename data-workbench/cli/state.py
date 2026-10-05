"""Shared constants, repo-root discovery, small helpers, and `.dwb/state.json`.

Kept dependency-free (stdlib only) so every other `cli/` module can import it
under the system python before a venv exists.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# cli/ lives directly under the repo root.
ROOT = Path(__file__).resolve().parent.parent
DWB_DIR = ROOT / ".dwb"
STATE_FILE = DWB_DIR / "state.json"
PID_DIR = ROOT / ".pids"
LOG_DIR = ROOT / ".logs"

# --- Ports (host-published) --------------------------------------------------
PORT_BACKEND = 8000
PORT_FRONTEND = 5173
PORT_NEO4J_HTTP = 7475   # compose-managed neo4j browser (host 7475 -> ctr 7474)
PORT_NEO4J_BOLT = 7688   # compose-managed neo4j bolt    (host 7688 -> ctr 7687)
PORT_PG = 5433
PORT_MYSQL = 3307
PORT_GITEA = 3101
PORT_S3 = 9000            # SeaweedFS S3 API      (host 9000 -> ctr 8333)
PORT_S3_CONSOLE = 8888    # SeaweedFS filer web UI (browse published files)
PORT_S3_MASTER = 9333     # SeaweedFS master UI
PORT_OTLP_GRPC = 4317     # OTel collector — OTLP gRPC ingest
PORT_OTLP_HTTP = 4318     # OTel collector — OTLP HTTP ingest (default target)
PORT_GRAFANA = 3111       # OTel collector — Grafana UI (host 3111 -> ctr 3000)

# Where host-mode looks for a reusable (shared) Neo4j before compose-managing one.
NEO4J_REUSE_HTTP = os.environ.get("NEO4J_HTTP", "http://localhost:7474")

# Compose service names.
SVC_NEO4J = "neo4j"
SVC_BACKEND = "backend"
SVC_FRONTEND = "frontend"
SVC_POSTGRES = "postgres"
SVC_MYSQL = "mysql-hr"
SVC_GITEA = "gitea"
SVC_SEAWEEDFS = "seaweedfs"
SVC_OTEL_COLLECTOR = "otel-collector"

# --- colours -----------------------------------------------------------------
_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
RED = "\033[0;31m" if _USE_COLOR else ""
GREEN = "\033[0;32m" if _USE_COLOR else ""
YELLOW = "\033[0;33m" if _USE_COLOR else ""
BLUE = "\033[0;34m" if _USE_COLOR else ""
DIM = "\033[2m" if _USE_COLOR else ""
NC = "\033[0m" if _USE_COLOR else ""


def info(msg: str) -> None:
    print(f"{BLUE}▸{NC} {msg}")


def ok(msg: str) -> None:
    print(f"  {GREEN}✓{NC} {msg}")


def warn(msg: str) -> None:
    print(f"  {YELLOW}!{NC} {msg}")


def err(msg: str) -> None:
    print(f"  {RED}✗{NC} {msg}")


def die(msg: str, code: int = 1) -> "None":
    print(f"{RED}error:{NC} {msg}", file=sys.stderr)
    sys.exit(code)


# --- subprocess --------------------------------------------------------------
def run(cmd: list[str], *, env: dict | None = None, cwd: Path | None = None,
        check: bool = True, capture: bool = False, quiet: bool = False) -> subprocess.CompletedProcess:
    """Run a command from the repo root. Streams by default; `capture=True`
    returns stdout/stderr on the result instead."""
    if not quiet and not capture:
        print(f"{DIM}$ {' '.join(cmd)}{NC}")
    full_env = {**os.environ, **(env or {})}
    return subprocess.run(
        cmd, cwd=str(cwd or ROOT), env=full_env, check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def which(name: str) -> bool:
    from shutil import which as _which
    return _which(name) is not None


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """True if something is already listening on host:port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.6)
        return s.connect_ex((host, port)) == 0


# --- state.json --------------------------------------------------------------
def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(mode: str, profiles: list[str],
               pg_samples: list[str], mysql_samples: list[str]) -> dict:
    DWB_DIR.mkdir(parents=True, exist_ok=True)
    state = {
        "mode": mode,
        "profiles": profiles,
        "pg_samples": pg_samples,
        "mysql_samples": mysql_samples,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")
    return state


def state_samples(st: dict, platform: str) -> list[str]:
    """Read the persisted sample-name list for a platform, back-compat with the
    old single ``pg_sample`` string."""
    if platform == "postgres":
        if "pg_samples" in st:
            return list(st.get("pg_samples") or [])
        legacy = st.get("pg_sample")
        return [legacy] if legacy else []
    return list(st.get("mysql_samples") or [])


def ensure_dirs() -> None:
    DWB_DIR.mkdir(parents=True, exist_ok=True)
    PID_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
