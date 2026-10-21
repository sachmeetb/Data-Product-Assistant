"""Auth endpoints — login, current-user, logout, and an unauthenticated config
probe the SPA reads before it has a token.

Mounted at ``/api/auth``. ``/login`` and ``/config`` are on the middleware
allowlist (no token required); ``/me`` requires a valid session.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .. import config
from ..auth import AuthUser, auth_enabled, current_user, get_provider, issue_token
from ..authz import client_hats

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: str
    password: str


@router.get("/config")
def auth_config():
    """Unauthenticated: whether auth is on + whether the instance is read-only.

    The SPA reads this before it has a token so it knows whether to render
    ``/login``.
    """
    return {"auth_enabled": auth_enabled(), "read_only": config.read_only()}


@router.post("/login")
def login(body: LoginRequest):
    """Authenticate against the configured provider → signed session JWT."""
    if not auth_enabled():
        # Auth off: no accounts exist / no secret to sign with. Be explicit
        # rather than minting an unsignable token.
        raise HTTPException(status_code=400, detail="Authentication is not enabled on this instance.")
    user = get_provider().authenticate(body.email, body.password)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    token = issue_token(user)
    return {
        "token": token,
        "email": user.email,
        "name": user.name,
        "role": user.role,
    }


@router.get("/me")
def me(request: Request, user: AuthUser = Depends(current_user)):
    """Current principal + the client-side hats the account may assume + the
    read-only flag. One call drives all frontend gating."""
    return {
        **user.as_dict(),
        "client_hats": client_hats(user.role),
        "read_only": config.read_only(),
        "auth_enabled": auth_enabled(),
    }


@router.post("/logout")
def logout():
    """Stateless — the client discards the token. (Denylist deferred.)"""
    return {"ok": True}
