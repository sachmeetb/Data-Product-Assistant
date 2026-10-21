"""Auth + authz + read-only tests.

Covers the pure-logic core (password hashing, JWT round-trip/expiry, the
role truth tables, the provider) plus the HTTP middleware (401 on missing
token, read-only 403 on mutations, allowlists) via a TestClient. All env is
set through monkeypatch so the rest of the suite stays auth-disabled.
"""

from __future__ import annotations

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from workbench.backend import auth, config
from workbench.backend.authz import role_can_review, role_can_run_stage
from workbench.backend.database import engine
from workbench.backend.models import AppUser


# ── password hashing ────────────────────────────────────────────────────────
def test_password_hash_roundtrip():
    h = auth.hash_password("hunter2")
    assert h.startswith("pbkdf2_")
    assert auth.verify_password("hunter2", h)
    assert not auth.verify_password("wrong", h)
    assert not auth.verify_password("hunter2", "not-a-hash")


# ── JWT round-trip + expiry ──────────────────────────────────────────────────
def test_jwt_roundtrip(monkeypatch):
    monkeypatch.setenv("WB_AUTH_SECRET", "s3cret")
    user = auth.AuthUser(email="a@b.com", name="A", role="owner")
    tok = auth.issue_token(user)
    decoded = auth.decode_token(tok)
    assert decoded is not None
    assert decoded.email == "a@b.com"
    assert decoded.role == "owner"


def test_jwt_expired(monkeypatch):
    monkeypatch.setenv("WB_AUTH_SECRET", "s3cret")
    monkeypatch.setenv("WB_AUTH_TOKEN_TTL_HOURS", "-1")  # exp in the past
    tok = auth.issue_token(auth.AuthUser(email="a@b.com", name="A", role="engineer"))
    assert auth.decode_token(tok) is None


def test_jwt_garbage_and_wrong_secret(monkeypatch):
    monkeypatch.setenv("WB_AUTH_SECRET", "s3cret")
    assert auth.decode_token("garbage.token.here") is None
    # token signed with a different secret must not verify
    forged = jwt.encode({"sub": "x@y.com", "role": "owner"}, "other", algorithm="HS256")
    assert auth.decode_token(forged) is None


def test_jwt_disabled_returns_none():
    # No WB_AUTH_SECRET → decode short-circuits to None.
    assert auth.decode_token("anything") is None


# ── auth_enabled gating ──────────────────────────────────────────────────────
def test_auth_enabled_gating(monkeypatch):
    monkeypatch.delenv("WB_AUTH_SECRET", raising=False)
    monkeypatch.delenv("WB_AUTH_REQUIRE", raising=False)
    assert auth.auth_enabled() is False
    monkeypatch.setenv("WB_AUTH_SECRET", "x")
    assert auth.auth_enabled() is True
    monkeypatch.delenv("WB_AUTH_SECRET")
    monkeypatch.setenv("WB_AUTH_REQUIRE", "1")
    assert auth.auth_enabled() is True


# ── StaticUserProvider ───────────────────────────────────────────────────────
def test_static_provider_authenticate():
    with Session(engine) as s:
        s.add(AppUser(email="u@x.com", name="U", role="engineer",
                      password_hash=auth.hash_password("pw"), active=True))
        s.commit()
    prov = auth.StaticUserProvider()
    assert prov.authenticate("u@x.com", "pw").role == "engineer"
    assert prov.authenticate("u@x.com", "bad") is None
    assert prov.authenticate("missing@x.com", "pw") is None


def test_static_provider_inactive_denied():
    with Session(engine) as s:
        s.add(AppUser(email="d@x.com", name="D", role="owner",
                      password_hash=auth.hash_password("pw"), active=False))
        s.commit()
    assert auth.StaticUserProvider().authenticate("d@x.com", "pw") is None


# ── role truth tables ────────────────────────────────────────────────────────
def test_role_can_run_stage_truth_table():
    # owner can run PO stages, not engineer stages
    assert role_can_run_stage("owner", "Data Product Owner") is True
    assert role_can_run_stage("owner", "Data Engineer") is False
    # engineer can run engineer-side hats, not PO stages
    assert role_can_run_stage("engineer", "Data Engineer") is True
    assert role_can_run_stage("engineer", "Data Steward") is True
    assert role_can_run_stage("engineer", "Data Quality Analyst") is True
    assert role_can_run_stage("engineer", "Data Product Owner") is False


def test_role_can_review_truth_table():
    # PO owns domain_rules + source_product_validation
    assert role_can_review("owner", "domain_rules") is True
    assert role_can_review("owner", "source_product_validation") is True
    assert role_can_review("owner", "mappings") is False
    # engineer owns the rest
    assert role_can_review("engineer", "mappings") is True
    assert role_can_review("engineer", "descriptions") is True
    assert role_can_review("engineer", "domain_rules") is False


# ── current_user dev default when auth disabled ──────────────────────────────
def test_current_user_dev_default(monkeypatch):
    monkeypatch.delenv("WB_AUTH_SECRET", raising=False)
    monkeypatch.delenv("WB_AUTH_REQUIRE", raising=False)

    class _Req:
        class state:  # noqa: N801
            pass

    u = auth.current_user(_Req())
    assert u.role == "engineer"  # dev default keeps local dev running


# ── HTTP middleware via TestClient ───────────────────────────────────────────
@pytest.fixture()
def client():
    from workbench.backend.main import app
    return TestClient(app)


def test_health_open_when_auth_on(client, auth_env, monkeypatch):
    # health + auth/config are allowlisted even with auth on
    assert client.get("/api/health").status_code == 200
    r = client.get("/api/auth/config")
    assert r.status_code == 200
    assert r.json()["auth_enabled"] is True


def test_missing_token_401(client, auth_env):
    # A GET to a non-allowlisted /api path with no token → 401.
    r = client.get("/api/settings")
    assert r.status_code == 401


def test_login_and_authenticated_request(client, auth_env):
    email, pw = auth_env["engineer"]
    r = client.post("/api/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200
    token = r.json()["token"]
    assert r.json()["role"] == "engineer"
    # /me with the token resolves the principal + client hats
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email"] == email
    assert "Data Engineer" in me.json()["client_hats"]


def test_bad_login_401(client, auth_env):
    email, _ = auth_env["owner"]
    r = client.post("/api/auth/login", json={"email": email, "password": "nope"})
    assert r.status_code == 401


def test_readonly_blocks_mutations_but_allows_login(client, monkeypatch):
    monkeypatch.setenv("WB_READ_ONLY", "1")
    # A non-GET /api call (not login) → 403 read_only
    r = client.post("/api/auth/logout")
    assert r.status_code == 403
    assert r.json()["read_only"] is True
    # login stays reachable (so a user can still sign in on a read-only box)
    lr = client.post("/api/auth/login", json={"email": "x@y.com", "password": "z"})
    assert lr.status_code != 403
    # GET reads still work
    assert client.get("/api/health").status_code == 200


def test_readonly_live_flip(client, monkeypatch):
    # read_only() is live-read: flipping the env changes behaviour w/o restart.
    monkeypatch.setenv("WB_READ_ONLY", "1")
    assert config.read_only() is True
    monkeypatch.setenv("WB_READ_ONLY", "0")
    assert config.read_only() is False
