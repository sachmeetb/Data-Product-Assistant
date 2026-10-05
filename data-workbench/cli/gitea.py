"""Gitea auto-bootstrap + repo cleanup.

After the `gitea` profile comes up healthy, `bootstrap()` makes Push-to-Git work
with zero manual steps: create the `workbench` admin user, generate a PAT, ensure
the `dataworkbench` org, and write it all into the backend's AppSettings via
`PUT /api/settings`. `list_repos` / `delete_repo` let `dwb reset --soft` clear the
org's product repos (git_provider.py has no delete), without nuking the volume.

The CLI runs on the host, so it talks to Gitea's published port (localhost:3101)
and the backend's published port (localhost:8000). The `git_base_url` it WRITES
differs by mode: backend→gitea is `http://gitea:3000` in compose (compose net) or
`http://localhost:3101` in host mode.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from . import compose, state

GITEA_API = f"http://localhost:{state.PORT_GITEA}/api/v1"
BACKEND = f"http://localhost:{state.PORT_BACKEND}"
ORG = "dataworkbench"
GIT_USER = "workbench"
GIT_PASSWORD = "workbenchpass"


def _api(method: str, url: str, *, token: str | None = None, body: dict | None = None,
         timeout: float = 8.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    if token:
        req.add_header("Authorization", f"token {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = raw
        return e.code, payload
    except (urllib.error.URLError, OSError) as e:
        return 0, str(e)


def _backend_settings() -> dict | None:
    status, payload = _api("GET", f"{BACKEND}/api/settings")
    return payload if status == 200 and isinstance(payload, dict) else None


def _create_admin_user() -> None:
    """Idempotent — `user create` fails loudly if the user exists; we swallow that."""
    res = compose.exec_svc(
        state.SVC_GITEA,
        ["gitea", "admin", "user", "create",
         "--username", GIT_USER, "--password", GIT_PASSWORD,
         "--email", "workbench@local", "--admin", "--must-change-password=false"],
        profiles=["gitea"], user="git", capture=True, check=False,
    )
    out = (res.stdout or "") + (res.stderr or "")
    if res.returncode == 0:
        state.ok(f"Gitea admin user '{GIT_USER}' created")
    elif "already exist" in out.lower():
        state.ok(f"Gitea admin user '{GIT_USER}' already present")
    else:
        state.warn(f"Gitea user create returned nonzero: {out.strip()[:200]}")


def _generate_token() -> str | None:
    # Token names must be unique per user; a fresh name avoids the "already
    # exists" failure on a re-bootstrap where the backend lost the token.
    name = f"dwb-{int(time.time())}"
    res = compose.exec_svc(
        state.SVC_GITEA,
        ["gitea", "admin", "user", "generate-access-token",
         "--username", GIT_USER,
         # read:user is required so a freshly-minted token passes
         # GiteaProvider.test_connection (GET /api/v1/user). read/write:org +
         # write:repository cover every push and org operation.
         "--scopes", "read:user,read:organization,write:organization,write:repository",
         "--token-name", name, "--raw"],
        profiles=["gitea"], user="git", capture=True, check=False,
    )
    if res.returncode != 0:
        state.warn(f"Gitea token generation failed: {(res.stderr or res.stdout or '').strip()[:200]}")
        return None
    token = (res.stdout or "").strip().splitlines()[-1].strip() if res.stdout else ""
    return token or None


def _ensure_org(token: str) -> None:
    status, _ = _api("GET", f"{GITEA_API}/orgs/{ORG}", token=token)
    if status == 200:
        state.ok(f"Gitea org '{ORG}' present")
        return
    status, payload = _api("POST", f"{GITEA_API}/orgs", token=token,
                           body={"username": ORG, "visibility": "public"})
    if status in (201, 200):
        state.ok(f"Gitea org '{ORG}' created")
    elif status == 422:  # already exists
        state.ok(f"Gitea org '{ORG}' present")
    else:
        state.warn(f"Could not ensure org '{ORG}' (HTTP {status}): {payload}")


def _write_settings(mode: str, token: str) -> None:
    git_base_url = "http://gitea:3000" if mode == "compose" else f"http://localhost:{state.PORT_GITEA}"
    body = {
        "git_provider": "gitea",
        "git_base_url": git_base_url,
        "git_web_base_url": f"http://localhost:{state.PORT_GITEA}",
        "git_org": ORG,
        "git_token": token,
    }
    status, payload = _api("PUT", f"{BACKEND}/api/settings", body=body)
    if status == 200:
        state.ok("Wrote Git integration settings into AppSettings")
    else:
        state.warn(f"Could not write Git settings to backend (HTTP {status}): {payload}")


def _token_healthy() -> bool:
    """Probe the stored PAT via the backend's auth check (GET /api/v1/user).
    Returns True only when the backend has a token AND it passes the probe —
    a scope-deficient token (no read:user) fails here even though push works,
    which is exactly the case we want to self-heal by re-minting."""
    status, payload = _api("GET", f"{BACKEND}/api/settings/git/test")
    return bool(status == 200 and isinstance(payload, dict) and payload.get("ok"))


def bootstrap(mode: str) -> None:
    """Full idempotent bootstrap. Reuses an existing PAT only when it still
    passes the backend's auth probe; a missing or scope-deficient token is
    re-minted with the full scopes (self-heals an existing box on `dwb up`)."""
    state.info("Bootstrapping Gitea (Push-to-Git)…")

    settings = _backend_settings()
    configured = bool(settings and settings.get("git_provider") == "gitea"
                      and settings.get("git_token_set"))

    _create_admin_user()

    if configured and _token_healthy():
        state.ok("Gitea already configured in AppSettings — existing PAT is healthy")
        # Org creation is idempotent at push time (git_provider.ensure_repo), so
        # with a working token there's nothing left to do.
        return
    if configured:
        state.warn("Stored Gitea PAT failed the auth probe (likely missing read:user) "
                   "— re-minting with full scopes")

    token = _generate_token()
    if not token:
        state.warn("Gitea bootstrap incomplete — Push-to-Git will need a manual PAT "
                   "(Settings → Applications → Generate Token).")
        return
    _ensure_org(token)
    _write_settings(mode, token)


# --- soft-reset repo cleanup -------------------------------------------------
def _mint_cleanup_token() -> str | None:
    return _generate_token()


def list_repos(token: str) -> list[str]:
    repos: list[str] = []
    page = 1
    while True:
        status, payload = _api("GET", f"{GITEA_API}/orgs/{ORG}/repos?limit=50&page={page}", token=token)
        if status != 200 or not isinstance(payload, list) or not payload:
            break
        repos += [r["name"] for r in payload if isinstance(r, dict) and r.get("name")]
        if len(payload) < 50:
            break
        page += 1
    return repos


def delete_repo(token: str, name: str) -> bool:
    status, _ = _api("DELETE", f"{GITEA_API}/repos/{ORG}/{name}", token=token)
    return status in (204, 200, 404)


def clear_org_repos() -> int:
    """Delete every repo under the org. Returns the count deleted. Best-effort —
    used by `dwb reset --soft` to clear product repos without dropping the volume."""
    token = _mint_cleanup_token()
    if not token:
        state.warn("Could not mint a Gitea token for repo cleanup — skipping.")
        return 0
    repos = list_repos(token)
    if not repos:
        state.ok("No Gitea org repos to clear")
        return 0
    deleted = 0
    for name in repos:
        if delete_repo(token, name):
            deleted += 1
    state.ok(f"Cleared {deleted}/{len(repos)} Gitea org repo(s)")
    return deleted
