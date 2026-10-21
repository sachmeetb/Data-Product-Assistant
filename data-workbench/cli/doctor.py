"""Preflight + post-up health checks.

`preflight()` runs before every `up` (and standalone as `dwb doctor`): docker
CLI + daemon, compose v2, ports free for the selected services/mode, host-mode
toolchain (venv / node / node_modules), and secrets (.env LLM key + MCP tokens —
a Foundry key with no Foundry resource is a hard failure, since config.py has no
default resource). Hard failures return False (and `up` aborts); everything else
is an advisory warning with an actionable fix. `wait_for_app()` polls health
after `up`.
"""
from __future__ import annotations

import time
import urllib.error
import urllib.request
from pathlib import Path

from . import state


def _docker_ok() -> bool:
    if not state.which("docker"):
        state.err("docker CLI not found — install Docker Desktop / Engine.")
        return False
    try:
        state.run(["docker", "info"], capture=True, check=True)
    except Exception:
        state.err("docker daemon not responding — start Docker and retry "
                  "(`docker info` must succeed).")
        return False
    state.ok("docker daemon")
    return True


def _compose_ok() -> bool:
    try:
        res = state.run(["docker", "compose", "version"], capture=True, check=True)
    except Exception:
        state.err("`docker compose` (v2) not available — install the Compose "
                  "plugin (the standalone `docker-compose` v1 is not supported).")
        return False
    state.ok(f"docker compose ({(res.stdout or '').strip().splitlines()[0] if res.stdout else 'v2'})")
    return True


def _expected_ports(mode: str, profiles: list[str]) -> list[tuple[int, str]]:
    ports = [(state.PORT_BACKEND, "backend"), (state.PORT_FRONTEND, "frontend")]
    if mode == "compose":
        # host-mode reuses/points at an existing neo4j; compose-managed neo4j
        # only when compose owns it (checked separately at launch).
        ports += [(state.PORT_NEO4J_HTTP, "neo4j http"),
                  (state.PORT_NEO4J_BOLT, "neo4j bolt")]
    if "postgres" in profiles:
        ports.append((state.PORT_PG, "postgres"))
    if "mysql" in profiles:
        ports.append((state.PORT_MYSQL, "mysql-hr"))
    if "gitea" in profiles:
        ports.append((state.PORT_GITEA, "gitea"))
    if "storage" in profiles:
        ports += [(state.PORT_S3, "object store (s3)"),
                  (state.PORT_S3_CONSOLE, "object store (console)"),
                  (state.PORT_S3_MASTER, "object store (master)")]
    if "observability" in profiles:
        ports += [(state.PORT_OTLP_GRPC, "otel grpc"),
                  (state.PORT_OTLP_HTTP, "otel http"),
                  (state.PORT_GRAFANA, "grafana")]
    return ports


def _ports_ok(mode: str, profiles: list[str], running: bool) -> None:
    """Advisory only. When the stack is already running its own ports are
    expected to be in use, so skip the noise in that case."""
    if running:
        return
    for port, label in _expected_ports(mode, profiles):
        if state.port_in_use(port):
            state.warn(f"port {port} ({label}) already in use — stop whatever holds "
                       f"it, or that service will fail to bind.")
        else:
            state.ok(f"port {port} free ({label})")


def _read_env_file() -> dict[str, str]:
    env: dict[str, str] = {}
    envfile = state.ROOT / ".env"
    if not envfile.is_file():
        return env
    for line in envfile.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _claude_code_nudge() -> None:
    """Extra hint for the case that motivated the token route: a developer who
    already has Claude Code on this workstation and no API key anywhere. The
    ambient login can't reach the backend container, but a token minted from it
    can. Silent when there's no local Claude Code login to point at."""
    if not (Path.home() / ".claude" / ".credentials.json").is_file():
        return
    state.warn("...but you DO have a Claude Code login on this machine. Turn it into "
               "a portable credential:  claude setup-token  — then put the printed "
               "value in .env as CLAUDE_CODE_OAUTH_TOKEN= (valid 1 year; it bills "
               "your own subscription, so keep it off shared boxes).")


def _secrets_ok(mode: str) -> bool:
    """Secrets/config checks. Returns True when no HARD failure was hit.

    Mode matters for the LLM route: host mode merges .env + os.environ into
    uvicorn (see hostmode._backend_env), so an ambient Anthropic login on the
    host genuinely works. Compose mode mounts no ~/.claude into the backend
    container, so only an explicit credential in .env/the shell env can reach
    it — which is what CLAUDE_CODE_OAUTH_TOKEN is for.
    """
    import os
    hard_ok = True
    envfile = state.ROOT / ".env"
    fe = _read_env_file()

    def present(key: str) -> bool:
        return bool((fe.get(key) or os.environ.get(key) or "").strip())

    if not envfile.is_file():
        state.warn(".env not found at repo root — copy .env.example and fill it in "
                   "(ANTHROPIC_FOUNDRY_API_KEY + ANTHROPIC_FOUNDRY_RESOURCE, or "
                   "ANTHROPIC_API_KEY, or CLAUDE_CODE_OAUTH_TOKEN; plus "
                   "WB_MCP_TOKENS). See docs/deployment-guide.md.")

    if present("ANTHROPIC_FOUNDRY_API_KEY"):
        # config.py has NO default resource (it must not carry one operator's
        # private Azure resource name), so the key alone is a misconfiguration
        # that would otherwise surface as an opaque 404 on every LLM stage.
        if present("ANTHROPIC_FOUNDRY_RESOURCE"):
            state.ok("LLM key (ANTHROPIC_FOUNDRY_API_KEY → Azure Foundry)")
        else:
            state.err("ANTHROPIC_FOUNDRY_API_KEY is set but ANTHROPIC_FOUNDRY_RESOURCE "
                      "is missing — there is no default. Set "
                      "ANTHROPIC_FOUNDRY_RESOURCE=<your-azure-foundry-resource> in .env "
                      "(see .env.example), or every LLM stage will fail.")
            hard_ok = False
    elif present("ANTHROPIC_API_KEY"):
        state.ok("LLM key (ANTHROPIC_API_KEY → Anthropic direct)")
    elif present("CLAUDE_CODE_OAUTH_TOKEN"):
        # Checked LAST because that mirrors the real runtime precedence: the
        # bundled CLI prefers Foundry, then the API key, then this token.
        state.ok("LLM key (CLAUDE_CODE_OAUTH_TOKEN → Claude Code subscription)")
    elif mode == "compose":
        state.warn("no LLM key — set ANTHROPIC_API_KEY, or "
                   "ANTHROPIC_FOUNDRY_API_KEY + ANTHROPIC_FOUNDRY_RESOURCE, in .env; "
                   "stages that call the model will fail. (An ambient Claude Code "
                   "login does NOT help under compose — no ~/.claude is mounted "
                   "into the backend container. Use a subscription TOKEN instead: "
                   "`claude setup-token`.)")
        _claude_code_nudge()
    else:
        state.warn("no LLM key — set ANTHROPIC_API_KEY, or "
                   "ANTHROPIC_FOUNDRY_API_KEY + ANTHROPIC_FOUNDRY_RESOURCE, in .env "
                   "(or have an ambient Anthropic login); stages that call the "
                   "model will fail.")
        _claude_code_nudge()

    if present("WB_MCP_TOKENS"):
        state.ok("MCP tokens (WB_MCP_TOKENS)")
    elif present("WB_MCP_ALLOW_INSECURE"):
        state.warn("MCP running OPEN (WB_MCP_ALLOW_INSECURE) — fine locally, never "
                   "on a shared/remote box.")
    else:
        state.warn("no WB_MCP_TOKENS and no WB_MCP_ALLOW_INSECURE — the /mcp tools "
                   "fail closed. Set one in .env if you drive the backend over MCP.")

    return hard_ok


def _host_toolchain_ok() -> bool:
    hard_ok = True
    if (state.ROOT / "env").is_dir():
        state.ok("python venv (env/)")
    else:
        state.warn("python venv missing — `dwb up --mode host` offers to create it "
                   "(python3.12 -m venv env && pip install -r workbench/backend/requirements.txt).")

    if state.which("node") and state.which("npm"):
        state.ok("node + npm")
    else:
        state.err("node/npm missing — required for host mode (install Node.js 20+).")
        hard_ok = False

    if (state.ROOT / "workbench" / "frontend" / "node_modules").is_dir():
        state.ok("frontend deps (node_modules)")
    else:
        state.warn("frontend deps missing — first `dwb up --mode host` runs `npm install`.")
    return hard_ok


def preflight(mode: str, profiles: list[str], *, running: bool = False) -> bool:
    """Run all preflight checks. Returns True when no HARD failure was hit."""
    state.info(f"Preflight ({mode} mode, profiles: {', '.join(profiles) or 'core only'})")
    hard_ok = True

    hard_ok &= _docker_ok()
    hard_ok &= _compose_ok()

    if mode == "host":
        hard_ok &= _host_toolchain_ok()

    _ports_ok(mode, profiles, running)
    hard_ok &= _secrets_ok(mode)

    if not hard_ok:
        state.err("preflight found blocking problems — fix the ✗ items above.")
    return hard_ok


def _http_ok(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return 200 <= resp.status < 500
    except (urllib.error.URLError, OSError):
        return False


def wait_for_app(timeout: float = 90.0) -> bool:
    """Poll backend /api/health then the frontend until both answer or timeout."""
    backend = f"http://localhost:{state.PORT_BACKEND}/api/health"
    frontend = f"http://localhost:{state.PORT_FRONTEND}"
    state.info("Waiting for the app to answer…")
    deadline = time.time() + timeout
    be = fe = False
    while time.time() < deadline:
        if not be and _http_ok(backend):
            be = True
            state.ok(f"backend healthy ({backend})")
        if not fe and _http_ok(frontend):
            fe = True
            state.ok(f"frontend serving ({frontend})")
        if be and fe:
            return True
        time.sleep(2)
    if not be:
        state.warn(f"backend not answering at {backend} yet — check `dwb logs backend`.")
    if not fe:
        state.warn(f"frontend not answering at {frontend} yet — check `dwb logs frontend`.")
    return be and fe


def cmd_doctor() -> int:
    """Standalone `dwb doctor` — reports against the last-used launch (or a
    compose-mode default on a fresh checkout)."""
    st = state.load_state()
    mode = st.get("mode", "compose")
    profiles = st.get("profiles", ["gitea"])
    return 0 if preflight(mode, profiles) else 1
