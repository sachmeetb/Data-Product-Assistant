"""Authentication core for the Data Workbench.

The REST API + WebSockets historically had **no** request-level auth — identity
was faked with hardcoded email constants and the 5 client-side roles were a
freely-switchable React state. This module adds real authentication so the
platform can be deployed more broadly, following the precedents already in the
repo: the live-read env-flag gate of ``request_guard.enforcement_enabled`` and
the bearer model of the MCP ``_BearerAuthMiddleware``.

Design:

* Two server-enforced **account roles** — ``owner`` (Data Product Owner) and
  ``engineer``. An ``engineer`` account may still assume the engineer-side hats
  (Steward / DQA / Reviewer) *client-side* (matches the shell split); the server
  enforces only the PO↔Engineer boundary. See :mod:`authz`.
* Credentials checked against the ``AppUser`` SQLModel table via a pluggable
  :class:`AuthProvider` — :class:`StaticUserProvider` now, an ``OIDCProvider``
  later without touching call sites.
* Sessions are signed HS256 JWTs (PyJWT, pure-python — no native build).
* Auth is **gated**: enabled iff ``WB_AUTH_SECRET`` is set (or
  ``WB_AUTH_REQUIRE=1`` to fail-closed). Unset secret → disabled + a startup
  warning, so local dev stays frictionless and unchanged.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import Request
from sqlmodel import Session, select

from . import config
from .database import engine
from .models import AppUser

_log = logging.getLogger("workbench.auth")

# Account roles enforced server-side (distinct from the finer client-side hats).
OWNER_ROLE = "owner"
ENGINEER_ROLE = "engineer"
ACCOUNT_ROLES = {OWNER_ROLE, ENGINEER_ROLE}

# Default dev identity when auth is disabled (keeps local dev unchanged: an
# engineer can drive the whole pipeline). Matches the old faked-engineer posture.
# Env-overridable so a shared/demo stack can ALIGN it with the frontend's
# auth-off fallback (`authStore.DEV_FALLBACK_EMAIL`) — otherwise the backend
# stamps a different identity (owner_email / submitted_by / reviewed_by) than the
# one the PO web view queries with, and intake-scaffolded products never surface.
_DEV_USER_EMAIL = os.environ.get("WB_DEV_USER_EMAIL", "").strip() or "dev@workbench.local"
_DEV_USER_NAME = os.environ.get("WB_DEV_USER_NAME", "").strip() or "Local Dev"


@dataclass
class AuthUser:
    """The authenticated principal for a request."""

    email: str
    name: str
    role: str  # "owner" | "engineer"

    def as_dict(self) -> dict:
        return {"email": self.email, "name": self.name, "role": self.role}


# ── Password hashing (stdlib pbkdf2 — no bcrypt/native dep) ─────────────────
_PBKDF2_ITERATIONS = 240_000
_PBKDF2_ALGO = "sha256"


def hash_password(password: str) -> str:
    """Return a self-describing ``pbkdf2_<algo>$<iters>$<salt_hex>$<hash_hex>``."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac(_PBKDF2_ALGO, password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_{_PBKDF2_ALGO}${_PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verify against a hash produced by :func:`hash_password`."""
    try:
        scheme, iters_s, salt_hex, hash_hex = stored.split("$", 3)
        if not scheme.startswith("pbkdf2_"):
            return False
        algo = scheme.split("_", 1)[1]
        iters = int(iters_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, TypeError):
        return False
    dk = hashlib.pbkdf2_hmac(algo, password.encode("utf-8"), salt, iters)
    return hmac.compare_digest(dk, expected)


# ── Provider seam (pluggable for future OIDC/SSO) ───────────────────────────
class AuthProvider(ABC):
    """Pluggability seam: swap the credential store without touching call sites."""

    @abstractmethod
    def authenticate(self, email: str, password: str) -> Optional[AuthUser]:
        ...

    @abstractmethod
    def get_user(self, email: str) -> Optional[AuthUser]:
        ...


class StaticUserProvider(AuthProvider):
    """Validate against the ``AppUser`` table (pbkdf2-hashed passwords)."""

    def authenticate(self, email: str, password: str) -> Optional[AuthUser]:
        email = (email or "").strip().lower()
        if not email or not password:
            return None
        with Session(engine) as session:
            row = session.get(AppUser, email)
        if row is None or not row.active:
            return None
        if not verify_password(password, row.password_hash):
            return None
        return AuthUser(email=row.email, name=row.name, role=row.role)

    def get_user(self, email: str) -> Optional[AuthUser]:
        email = (email or "").strip().lower()
        if not email:
            return None
        with Session(engine) as session:
            row = session.get(AppUser, email)
        if row is None or not row.active:
            return None
        return AuthUser(email=row.email, name=row.name, role=row.role)


_provider: AuthProvider = StaticUserProvider()


def get_provider() -> AuthProvider:
    return _provider


def set_provider(provider: AuthProvider) -> None:
    """Install a different provider (e.g. an OIDCProvider). Kept for the seam."""
    global _provider
    _provider = provider


# ── Auth gating (live-read, mirrors request_guard.enforcement_enabled) ──────
def auth_enabled() -> bool:
    """True when request-level auth is active.

    Enabled iff ``WB_AUTH_SECRET`` is set, OR ``WB_AUTH_REQUIRE`` is truthy
    (fail-closed opt-in — makes a missing secret an error rather than silently
    disabling auth). Read live so tests/operators can flip it without restart.
    """
    if config.auth_secret():
        return True
    return os.environ.get("WB_AUTH_REQUIRE", "").strip().lower() in ("1", "true", "yes", "on")


def _secret_or_raise() -> str:
    secret = config.auth_secret()
    if not secret:
        # WB_AUTH_REQUIRE without a secret: fail loudly rather than sign with a
        # guessable key.
        raise RuntimeError(
            "WB_AUTH_REQUIRE is set but WB_AUTH_SECRET is empty — cannot sign "
            "or verify session tokens. Set WB_AUTH_SECRET."
        )
    return secret


# ── JWT ─────────────────────────────────────────────────────────────────────
def issue_token(user: AuthUser) -> str:
    """Sign an HS256 session JWT for ``user`` (TTL from WB_AUTH_TOKEN_TTL_HOURS)."""
    secret = _secret_or_raise()
    now = datetime.now(timezone.utc)
    claims = {
        "sub": user.email,
        "name": user.name,
        "role": user.role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=config.token_ttl_hours())).timestamp()),
    }
    return jwt.encode(claims, secret, algorithm="HS256")


def decode_token(token: str) -> Optional[AuthUser]:
    """Verify + decode a session JWT. Returns None on any failure (bad sig,
    expired, malformed, missing claims)."""
    secret = config.auth_secret()
    if not secret or not token:
        return None
    try:
        claims = jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
    email = claims.get("sub")
    role = claims.get("role")
    if not email or role not in ACCOUNT_ROLES:
        return None
    return AuthUser(email=email, name=claims.get("name") or email, role=role)


# ── FastAPI dependency ──────────────────────────────────────────────────────
def dev_user() -> AuthUser:
    """The default identity used when auth is disabled (local dev)."""
    return AuthUser(email=_DEV_USER_EMAIL, name=_DEV_USER_NAME, role=ENGINEER_ROLE)


def current_user(request: Request) -> AuthUser:
    """FastAPI dependency: the authenticated user for this request.

    Reads ``request.state.user`` (set by the auth middleware). When auth is
    disabled, returns the dev default so existing endpoints work unchanged.
    """
    user = getattr(request.state, "user", None)
    if isinstance(user, AuthUser):
        return user
    if not auth_enabled():
        return dev_user()
    # Auth enabled but no user on state — the middleware should have 401'd
    # already; this is a defensive fallback for routes it didn't cover.
    from fastapi import HTTPException
    raise HTTPException(status_code=401, detail="Authentication required")
