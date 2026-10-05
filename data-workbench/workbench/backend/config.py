import logging
import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent.parent
BASE_PROJECT_DIR = BASE_DIR / "projects"
# Env-overridable so a container can point SQLite at a mounted volume
# (e.g. WB_DATABASE_URL=sqlite:////app/data/workbench.db) for persistence.
DATABASE_URL = os.environ.get(
    "WB_DATABASE_URL", f"sqlite:///{BASE_DIR / 'workbench.db'}"
)

# Public base URL of the frontend (PO/engineer web UI). Used to compose
# deep links the MCP control plane hands back to an engineer's Claude Code
# (e.g. "open the project dashboard"). Env-overridable for container/remote
# deployments where the UI isn't on localhost:5173.
FRONTEND_URL = os.environ.get("WB_FRONTEND_URL", "http://localhost:5173").rstrip("/")

# Public base URL of the BACKEND itself (this API's own origin, e.g.
# http://localhost:8000). Baked into the served kit installers + bootstrap
# prompt so they fetch the bundle and register the MCP endpoint against a
# trusted origin. When unset, the installer routers fall back to the incoming
# request's base_url (fine for local dev); set WB_PUBLIC_BASE_URL on any shared/
# remote deploy so a forged Host header can't redirect the installer's fetches.
# NOTE: distinct from FRONTEND_URL — the kit needs the backend origin, not the UI.
PUBLIC_BASE_URL = os.environ.get("WB_PUBLIC_BASE_URL", "").rstrip("/")

# Quick-connect provisioning manifest written by the `dwb` launcher CLI (see
# cli/connect.py). A JSON list of launched sample-DB coordinates the dev router
# (routers/dev.py) serves to the frontend connection forms for one-click prefill.
# Repo-root .dwb/ by default; the container overrides to the mounted /app/.dwb/.
# Absent file / read-only instance → the endpoint returns []. Demo creds only.
PROVISIONED_SOURCES_PATH = os.environ.get(
    "WB_PROVISIONED_SOURCES", str(BASE_DIR / ".dwb" / "provisioned-sources.json")
)


def public_base_url(request) -> str:
    """Trusted backend origin for served installer/bootstrap content: the
    configured WB_PUBLIC_BASE_URL, else the request's Host-derived base_url."""
    return PUBLIC_BASE_URL or str(request.base_url).rstrip("/")

# --- Authentication & read-only mode ----------------------------------------
# Request-level auth is GATED on WB_AUTH_SECRET being set (see auth.auth_enabled):
# unset → auth disabled + a startup warning, so local dev is frictionless. The
# secret signs HS256 session JWTs; set a long random value on any shared deploy.
def auth_secret() -> str:
    """The JWT signing secret (live-read so tests/operators can flip auth)."""
    return os.environ.get("WB_AUTH_SECRET", "").strip()


def token_ttl_hours() -> float:
    """Session-token lifetime in hours (WB_AUTH_TOKEN_TTL_HOURS, default 12)."""
    try:
        return float(os.environ.get("WB_AUTH_TOKEN_TTL_HOURS") or "12")
    except (TypeError, ValueError):
        return 12.0


def read_only() -> bool:
    """Global read-only mode (WB_READ_ONLY). Read LIVE so a hosted/demo box can
    flip it without a restart. Blocks every mutation for everyone (REST, WS,
    MCP) and drives the frontend banner + disabled controls."""
    return os.environ.get("WB_READ_ONLY", "").strip().lower() in ("1", "true", "yes", "on")


def transform_enforcement() -> str:
    """Staged transform-capability enforcement (WB_TRANSFORM_ENFORCEMENT).

    Read LIVE so it can flip without a restart (mirrors read_only()). Values:
      - ``off``   — the compiler gate is disabled (no preflight cost on deploy).
      - ``warn``  — DEFAULT — the gate runs and logs capability errors but never
                    blocks (safe to ship; visibility lives in the Phase-5 editor
                    callout + preflight endpoint).
      - ``block`` — the gate refuses to deploy/build when a transform expression
                    is unsupported on the target platform (clean 422). Flip to
                    this only after the portfolio audit (transform_audit) is clean.

    An unrecognised value falls back to ``warn``.
    """
    v = os.environ.get("WB_TRANSFORM_ENFORCEMENT", "warn").strip().lower()
    return v if v in ("off", "warn", "block") else "warn"


def intake_tokens() -> dict[str, str]:
    """Scoped machine credentials for the inbound-intake submit endpoint.

    Distinct from user JWTs and from WB_MCP_TOKENS — an external assessment tool
    authenticates with one of these bearer tokens and its ``source_system``
    (provenance) is DERIVED from the token, never trusted from the request body.

    Format: ``WB_INTAKE_TOKENS=token1:source_system_a,token2:source_system_b``.
    Returns a ``{token: source_system}`` map. Empty (fail-closed) unless
    ``WB_INTAKE_ALLOW_INSECURE=1`` seeds a single dev token for local testing.
    """
    raw = os.environ.get("WB_INTAKE_TOKENS", "").strip()
    tokens: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        tok, _, src = pair.partition(":")
        tok = tok.strip()
        src = src.strip()
        if tok and src:
            tokens[tok] = src
    if not tokens and os.environ.get("WB_INTAKE_ALLOW_INSECURE", "").strip().lower() in (
        "1", "true", "yes", "on",
    ):
        tokens["dev-intake-token"] = "dev-intake"
    return tokens


# CORS allow-list. Historically hardcoded to localhost — env-driven now so a
# hosted frontend origin is allowed without a code change. WB_FRONTEND_URL is
# always included; WB_CORS_ORIGINS is an optional comma-separated addition.
def cors_origins() -> list[str]:
    origins = ["http://localhost:5173", "http://localhost:3000"]
    if FRONTEND_URL and FRONTEND_URL not in origins:
        origins.append(FRONTEND_URL)
    extra = os.environ.get("WB_CORS_ORIGINS", "")
    for o in extra.split(","):
        o = o.strip().rstrip("/")
        if o and o not in origins:
            origins.append(o)
    return origins


# Workbench skills are packaged as an in-repo Claude Code *plugin* at
# ``workbench-skills/`` (``.claude-plugin/plugin.json`` + ``skills/<name>/``),
# version-controlled and baked into the Docker image via the blanket ``COPY . .``.
# They deliberately live OUTSIDE ``.claude/skills/`` so the interactive Claude
# Code session used to develop the Workbench does NOT auto-discover them as slash
# commands; the backend Agent SDK loads them explicitly via ``--plugin-dir``
# (see ``PIPELINE_PLUGINS`` below, threaded into every ``ClaudeAgentOptions``).
#
# Derived from BASE_DIR so paths stay correct inside a Docker image regardless of
# the checkout path. Skills' own SKILL.md scripts resolve via the runtime
# ${CLAUDE_SKILL_DIR} substitution (plugin-packaging-agnostic); backend code that
# spawns/points at skill scripts uses ``SKILLS_DIR``.
WORKBENCH_PLUGIN_DIR = BASE_DIR / "workbench-skills"
SKILLS_DIR = WORKBENCH_PLUGIN_DIR / "skills"

# Passed to every backend ClaudeAgentOptions(...) so the SDK loads the workbench
# skills from the plugin path (independent of setting_sources / .claude/skills
# discovery). ``path`` must be absolute — BASE_DIR is already ``.resolve()``'d.
PIPELINE_PLUGINS = [{"type": "local", "path": str(WORKBENCH_PLUGIN_DIR)}]

BASE_PROJECT_DIR.mkdir(exist_ok=True)


# --- Shared env helper ------------------------------------------------------
# NB (load-bearing): docker-compose injects "${VAR:-}" as an EMPTY string, and
# os.environ.setdefault will NOT overwrite an empty value — so both env blocks
# below use this blank-aware setter, which treats "" / whitespace as unset.
# Every var they *read* is likewise read blank-tolerantly.
def _set_env_if_blank(key: str, value: str) -> None:
    if not (os.environ.get(key) or "").strip():
        os.environ[key] = value


# --- LLM engine auth (Claude Agent SDK) -------------------------------------
# The Agent SDK bundles the Claude Code binary and resolves auth from process
# env vars, which it passes to every skill/stage/chat subprocess it spawns.
#
# Routing precedence (all secret-free in this file — the key comes from the
# environment, never hardcoded):
#   * If ANTHROPIC_FOUNDRY_API_KEY is set  -> route through Azure AI Foundry.
#   * Else if ANTHROPIC_API_KEY is set     -> direct Anthropic API.
#   * Else if CLAUDE_CODE_OAUTH_TOKEN is set -> bill a workstation's own Claude
#     Code SUBSCRIPTION. A long-lived (1yr) token from `claude setup-token`; the
#     only subscription route that survives containerisation, since the backend
#     container has no ~/.claude to authenticate from.
#   * Else                                 -> fall back to the ambient Claude
#     Code session on this host (host mode only — there is no ambient login
#     inside the container).
#
# Nothing here is armed unless the Foundry key is present: DW does NOT assume
# Azure. Routes 2 and 3 need NO code at all — the Agent SDK spawns the bundled
# Claude Code CLI with this process's env, and the CLI resolves ANTHROPIC_API_KEY
# / CLAUDE_CODE_OAUTH_TOKEN / the ambient login itself. A blank value is treated
# as unset by the CLI, so compose's `${VAR:-}` empty-string forwarding is safe
# and no blank-stripping is needed here.
#
# The Foundry RESOURCE is operator configuration with NO default — it names one
# tenant's private Azure resource, so it belongs in the gitignored .env (see
# .env.example), never committed in source. The MODEL keeps a default because
# it's a public model name, not a private identifier.
ANTHROPIC_FOUNDRY_RESOURCE = (os.environ.get("ANTHROPIC_FOUNDRY_RESOURCE") or "").strip()
if os.environ.get("ANTHROPIC_FOUNDRY_API_KEY"):
    if not ANTHROPIC_FOUNDRY_RESOURCE:
        # Fail LOUDLY, not silently — but deliberately do NOT raise: this module
        # is imported at load time by the app, both MCP servers, the test
        # conftest, and every scripts/*.py, so an import-time raise would break
        # unrelated entry points. `dwb doctor` (cli/doctor.py) is the real
        # pre-launch gate. Foundry is still armed: setting the key IS the
        # operator's declared intent, and silently flipping to direct-Anthropic
        # would only produce a second, more confusing auth error.
        logging.error(
            "ANTHROPIC_FOUNDRY_API_KEY is set but ANTHROPIC_FOUNDRY_RESOURCE is "
            "empty — every LLM stage will fail against Foundry. Set "
            "ANTHROPIC_FOUNDRY_RESOURCE=<your-azure-foundry-resource> in the "
            "repo-root .env (see .env.example)."
        )
    _set_env_if_blank("CLAUDE_CODE_USE_FOUNDRY", "1")
    if ANTHROPIC_FOUNDRY_RESOURCE:
        # Write the stripped value back so every SDK subprocess inherits it clean.
        os.environ["ANTHROPIC_FOUNDRY_RESOURCE"] = ANTHROPIC_FOUNDRY_RESOURCE
    # Foundry only serves the model *deployments* that exist on the resource.
    # A single-deployment resource means every tier (main / opus / sonnet /
    # haiku-fast) has to point at the same name — otherwise the SDK's default
    # Sonnet pick (and background Haiku calls) 404 with DeploymentNotFound and
    # every stage fails. Splitting the tiers is a separate opt-in feature
    # (ANTHROPIC_FOUNDRY_{OPUS,SONNET,HAIKU}_MODEL); it must NOT be done by
    # honouring the generic ANTHROPIC_DEFAULT_*_MODEL vars, which would
    # reintroduce the stale-ambient-value bug the override below guards against.
    #
    # Override (NOT setdefault): a stale ambient ANTHROPIC_DEFAULT_*_MODEL
    # (e.g. claude-sonnet-4-6 from a shell profile) would otherwise win and
    # break every stage even though that model isn't deployed here.
    _foundry_model = (
        os.environ.get("ANTHROPIC_FOUNDRY_MODEL") or ""
    ).strip() or "claude-opus-4-8"
    os.environ["ANTHROPIC_MODEL"] = _foundry_model
    os.environ["ANTHROPIC_DEFAULT_OPUS_MODEL"] = _foundry_model
    os.environ["ANTHROPIC_DEFAULT_SONNET_MODEL"] = _foundry_model
    os.environ["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = _foundry_model


# --- Observability / OpenTelemetry (opt-in) ---------------------------------
# DW emits telemetry ONLY when an OTLP endpoint is configured — the standard
# OTEL_EXPORTER_OTLP_ENDPOINT is the single on-switch (spec-idiomatic). Unset or
# blank => nothing is emitted (no cost, no export, no behaviour change). When it
# IS set we (1) turn on the Claude Code CLI's built-in OTel export — the Agent
# SDK spawns that CLI inheriting THIS process's env (see the SDK's
# subprocess_cli transport), so setting the vars here reaches every stage/chat/
# advisor subprocess — and (2) arm the backend's own exporters (telemetry.py).
#
# Every OTEL_* var stays operator-overridable, so the endpoint can point at ANY
# OTLP backend (Honeycomb / Datadog / Grafana Cloud / a self-hosted collector /
# the bundled `dwb up --with observability` reference collector).
#
# Every OTEL_* var below is written through the shared blank-aware
# _set_env_if_blank (defined above the LLM-auth block) so a compose-injected
# empty string is treated as unset.
def otel_endpoint() -> str:
    """The configured OTLP endpoint (empty string when telemetry is off).
    Live-read so tests/operators can flip it without a restart."""
    return (os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or "").strip()


def telemetry_enabled() -> bool:
    """True iff an OTLP endpoint is configured. The single opt-in switch."""
    return bool(otel_endpoint())


if telemetry_enabled():
    # CLI master switch + per-signal OTLP exporters (blank-aware so an operator
    # can still override any individual signal via env).
    _set_env_if_blank("CLAUDE_CODE_ENABLE_TELEMETRY", "1")
    _set_env_if_blank("OTEL_TRACES_EXPORTER", "otlp")
    _set_env_if_blank("OTEL_METRICS_EXPORTER", "otlp")
    _set_env_if_blank("OTEL_LOGS_EXPORTER", "otlp")
    # HTTP/protobuf on :4318 by default — keeps the backend exporter off grpcio
    # (no native build) and is proxy-friendly. The reference collector also
    # exposes :4317 (grpc), so an operator can switch protocol+port.
    _set_env_if_blank("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    # Claude Code TRACES are BETA and gated behind this flag; metrics/logs are
    # not. Enabled by default (a reference impl wants traces); overridable.
    _set_env_if_blank("CLAUDE_CODE_ENHANCED_TELEMETRY_BETA", "1")
    # Shared resource attribute so the CLI's spans/metrics and the backend's land
    # under one namespace. We deliberately do NOT set OTEL_SERVICE_NAME (that
    # would rename BOTH the CLI and the backend); the backend sets its own
    # service.name in code (telemetry.py), the CLI keeps its default.
    _existing_otel_attrs = (os.environ.get("OTEL_RESOURCE_ATTRIBUTES") or "").strip().strip(",")
    if "service.namespace" not in _existing_otel_attrs:
        os.environ["OTEL_RESOURCE_ATTRIBUTES"] = (
            f"{_existing_otel_attrs},service.namespace=data-workbench"
            if _existing_otel_attrs else "service.namespace=data-workbench"
        )
    # Tighter metric export cadence so token/cost counters show up promptly in a
    # dashboard (OTLP default is 60s). Operator-overridable.
    _set_env_if_blank("OTEL_METRIC_EXPORT_INTERVAL", "10000")
    # NOTE: OTEL_LOG_USER_PROMPTS is deliberately NOT set here — prompt/response
    # CONTENT capture stays OFF by default (prompts embed Neo4j connection
    # details). An operator opts in explicitly via env if they accept the risk.
