"""Git provider abstraction for pushing a data product's serving artifacts to a
remote repository (self-hosted Gitea or GitHub).

One repo per data product (named after ``project_code``). The push is a series of
`Contents API` `PUT`s — no local clone, no `git` binary, stdlib + ``httpx`` only.
Both providers speak the same REST shape for the four operations we need
(`test_connection`, `ensure_repo`, `push_files`, `repo_url`); the differences are
the base URL, the auth header, and the tag-creation endpoint.

Wired from ``routers/serving.py`` (the ``push-to-git`` endpoint + the auto-push
hook) via :func:`get_provider`, which reads the git fields off ``AppSettings``.
Returns ``None`` when git integration is not configured — callers treat that as a
disabled feature, never an error.
"""
from __future__ import annotations

import base64
from abc import ABC, abstractmethod
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

_TIMEOUT = 30.0

# When a self-hosted Gitea is reached over a docker-compose network the backend
# uses an internal hostname (``http://gitea:3000``) that a laptop browser can't
# resolve. If no explicit browser-facing base is configured, fall back to the
# port our compose publishes for web access.
_DEFAULT_GITEA_BROWSE_URL = "http://localhost:3101"


class GitProviderError(RuntimeError):
    """Raised when a git provider operation fails in a way worth surfacing."""


class GitProvider(ABC):
    """Minimal contents-API surface shared by Gitea and GitHub."""

    provider_id: str = "git"

    @abstractmethod
    def test_connection(self) -> dict:
        """Return ``{"ok": bool, "detail"|"error": str}`` — cheap auth probe."""

    @abstractmethod
    def ensure_repo(self, org: str, repo: str) -> str:
        """Create the repo (and its org) if missing. Returns the web URL."""

    @abstractmethod
    def push_files(
        self, org: str, repo: str, files: dict[str, object], message: str,
        tag: Optional[str] = None, author: Optional[dict] = None,
    ) -> str:
        """Commit every file via the contents API. Returns the last commit sha."""

    @abstractmethod
    def repo_url(self, org: str, repo: str) -> str:
        """The browseable web URL for the repo."""

    # shared helper -----------------------------------------------------------
    @staticmethod
    def _encode(content: object) -> str:
        raw = content if isinstance(content, bytes) else str(content).encode("utf-8")
        return base64.b64encode(raw).decode("ascii")


class GiteaProvider(GitProvider):
    """Self-hosted Gitea via ``/api/v1``. Token auth (``token <PAT>``)."""

    provider_id = "gitea"

    def __init__(self, base_url: str, token: str):
        if not base_url or not token:
            raise GitProviderError("Gitea requires both a base URL and a token.")
        self._web = base_url.rstrip("/")
        self.api = self._web + "/api/v1"
        self.headers = {
            "Authorization": f"token {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def test_connection(self) -> dict:
        try:
            # /version is unauthenticated; /user proves the token works.
            r = httpx.get(f"{self.api}/user", headers=self.headers, timeout=_TIMEOUT)
            if r.status_code == 200:
                who = r.json().get("login") or r.json().get("username") or "?"
                return {"ok": True, "detail": f"authenticated as {who}"}
            return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        except Exception as exc:  # noqa: BLE001 — surface any connect error verbatim
            return {"ok": False, "error": str(exc)}

    def ensure_repo(self, org: str, repo: str) -> str:
        r = httpx.get(f"{self.api}/repos/{org}/{repo}", headers=self.headers, timeout=_TIMEOUT)
        if r.status_code == 200:
            return self.repo_url(org, repo)
        if r.status_code != 404:
            raise GitProviderError(f"repo lookup failed: HTTP {r.status_code}: {r.text[:200]}")
        # Ensure the org exists (422 = already exists → fine), then create the repo
        # under it. If the "org" is actually a user, fall back to the user repos API.
        httpx.post(f"{self.api}/orgs", headers=self.headers,
                   json={"username": org, "visibility": "private"}, timeout=_TIMEOUT)
        create = httpx.post(
            f"{self.api}/orgs/{org}/repos", headers=self.headers,
            json={"name": repo, "private": True, "auto_init": True, "default_branch": "main"},
            timeout=_TIMEOUT,
        )
        if create.status_code >= 400:
            # org path failed (e.g. org is a personal user) — try the user repos API.
            create = httpx.post(
                f"{self.api}/user/repos", headers=self.headers,
                json={"name": repo, "private": True, "auto_init": True, "default_branch": "main"},
                timeout=_TIMEOUT,
            )
        if create.status_code >= 400 and create.status_code != 409:
            raise GitProviderError(f"repo create failed: HTTP {create.status_code}: {create.text[:200]}")
        return self.repo_url(org, repo)

    def push_files(self, org, repo, files, message, tag=None, author=None) -> str:
        sha = None
        for path, content in files.items():
            body: dict[str, object] = {
                "message": message, "content": self._encode(content), "branch": "main",
            }
            if author:
                body["author"] = author
                body["committer"] = author
            r = self._write_file(org, repo, path, body)
            sha = (r.json().get("commit") or {}).get("sha") or sha
        if tag and sha:
            # Best-effort tag — don't fail the push if tagging trips (e.g. exists).
            httpx.post(f"{self.api}/repos/{org}/{repo}/tags", headers=self.headers,
                       json={"tag_name": tag, "target": sha, "message": tag}, timeout=_TIMEOUT)
        return sha or ""

    def _write_file(self, org: str, repo: str, path: str, body: dict) -> httpx.Response:
        """Create or update one file. Gitea — unlike GitHub — splits the contents
        API: ``POST`` creates a new file, ``PUT`` updates an existing one and
        REQUIRES its blob sha (else ``422 [SHA]: Required``). Pick the verb by
        existence, and retry once the other way if a concurrent push (e.g. the
        auto-push daemon racing a manual push) flips the file's existence between
        our check and our write."""
        url = f"{self.api}/repos/{org}/{repo}/contents/{path}"

        def _existing_sha() -> Optional[str]:
            g = httpx.get(url, headers=self.headers, timeout=_TIMEOUT)
            return g.json().get("sha") if g.status_code == 200 else None

        sha = _existing_sha()
        r: Optional[httpx.Response] = None
        for attempt in range(2):
            payload = dict(body)
            if sha:
                payload["sha"] = sha
                r = httpx.put(url, headers=self.headers, json=payload, timeout=_TIMEOUT)
            else:
                r = httpx.post(url, headers=self.headers, json=payload, timeout=_TIMEOUT)
            if r.status_code < 400:
                return r
            # 409/422: existence flipped under us — re-fetch the sha and retry once.
            if attempt == 0 and r.status_code in (409, 422):
                sha = _existing_sha()
                continue
            raise GitProviderError(f"push {path} failed: HTTP {r.status_code}: {r.text[:200]}")
        return r  # unreachable — loop either returns or raises

    def repo_url(self, org: str, repo: str) -> str:
        return f"{self._web}/{org}/{repo}"


class GitHubProvider(GitProvider):
    """GitHub REST API (``api.github.com``). Bearer auth."""

    provider_id = "github"

    def __init__(self, token: str):
        if not token:
            raise GitProviderError("GitHub requires a token.")
        self.api = "https://api.github.com"
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def test_connection(self) -> dict:
        try:
            r = httpx.get(f"{self.api}/user", headers=self.headers, timeout=_TIMEOUT)
            if r.status_code == 200:
                return {"ok": True, "detail": f"authenticated as {r.json().get('login', '?')}"}
            return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    def ensure_repo(self, org: str, repo: str) -> str:
        r = httpx.get(f"{self.api}/repos/{org}/{repo}", headers=self.headers, timeout=_TIMEOUT)
        if r.status_code == 200:
            return self.repo_url(org, repo)
        if r.status_code != 404:
            raise GitProviderError(f"repo lookup failed: HTTP {r.status_code}: {r.text[:200]}")
        create = httpx.post(f"{self.api}/orgs/{org}/repos", headers=self.headers,
                            json={"name": repo, "private": True, "auto_init": True}, timeout=_TIMEOUT)
        if create.status_code >= 400:
            # org create path failed (org may be the authed user) — use /user/repos.
            create = httpx.post(f"{self.api}/user/repos", headers=self.headers,
                                json={"name": repo, "private": True, "auto_init": True}, timeout=_TIMEOUT)
        if create.status_code >= 400:
            raise GitProviderError(f"repo create failed: HTTP {create.status_code}: {create.text[:200]}")
        return self.repo_url(org, repo)

    def push_files(self, org, repo, files, message, tag=None, author=None) -> str:
        sha = None
        for path, content in files.items():
            body: dict[str, object] = {"message": message, "content": self._encode(content)}
            existing = httpx.get(f"{self.api}/repos/{org}/{repo}/contents/{path}",
                                 headers=self.headers, timeout=_TIMEOUT)
            if existing.status_code == 200:
                body["sha"] = existing.json().get("sha")
            if author:
                body["author"] = author
                body["committer"] = author
            r = httpx.put(f"{self.api}/repos/{org}/{repo}/contents/{path}",
                          headers=self.headers, json=body, timeout=_TIMEOUT)
            if r.status_code >= 400:
                raise GitProviderError(f"push {path} failed: HTTP {r.status_code}: {r.text[:200]}")
            sha = (r.json().get("commit") or {}).get("sha") or sha
        if tag and sha:
            httpx.post(f"{self.api}/repos/{org}/{repo}/git/refs", headers=self.headers,
                       json={"ref": f"refs/tags/{tag}", "sha": sha}, timeout=_TIMEOUT)
        return sha or ""

    def repo_url(self, org: str, repo: str) -> str:
        return f"https://github.com/{org}/{repo}"


def browse_url(repo_url: Optional[str], settings) -> Optional[str]:
    """Rewrite a stored repo URL (built from the backend-facing ``git_base_url``,
    e.g. ``http://gitea:3000/org/repo``) into a browser-reachable one for the UI's
    "View in Git" links.

    Precedence: the configured ``git_web_base_url`` origin wins. Otherwise a
    self-hosted Gitea whose host isn't already localhost defaults to
    :data:`_DEFAULT_GITEA_BROWSE_URL`. GitHub URLs (and anything already on
    localhost) are browser-correct and pass through untouched. A path on the
    browse base (e.g. behind a reverse proxy at ``/git``) is preserved as a prefix.
    """
    if not repo_url:
        return repo_url
    web_base = (getattr(settings, "git_web_base_url", None) or "").strip()
    if not web_base:
        provider = (getattr(settings, "git_provider", None) or "").strip().lower()
        host = (urlsplit(repo_url).hostname or "").lower()
        if provider != "gitea" or host in ("localhost", "127.0.0.1"):
            return repo_url
        web_base = _DEFAULT_GITEA_BROWSE_URL
    parts = urlsplit(repo_url)
    base = urlsplit(web_base)
    if not base.netloc:  # a malformed browse base — don't mangle the URL
        return repo_url
    prefix = base.path.rstrip("/")
    path = f"{prefix}{parts.path}" if prefix.strip("/") else parts.path
    return urlunsplit((base.scheme or parts.scheme, base.netloc, path,
                       parts.query, parts.fragment))


def get_provider(settings) -> Optional[GitProvider]:
    """Build a :class:`GitProvider` from ``AppSettings`` git fields, or ``None`` if
    git integration isn't configured. Never raises for a missing config — only for
    a present-but-invalid one (delegated to the provider ctor)."""
    provider = (getattr(settings, "git_provider", None) or "").strip().lower()
    if provider == "gitea":
        return GiteaProvider(getattr(settings, "git_base_url", "") or "",
                             getattr(settings, "git_token", "") or "")
    if provider == "github":
        return GitHubProvider(getattr(settings, "git_token", "") or "")
    return None
