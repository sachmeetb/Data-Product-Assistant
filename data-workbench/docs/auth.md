# Authentication, authorization & read-only mode

Basic, extensible request-level auth for the Data Workbench, added so the
platform can be deployed beyond a single-user local box. It deliberately mirrors
the precedents already in the repo — the live-read env-flag gate of
`request_guard`, the bearer model of the MCP `_BearerAuthMiddleware`, and the
`_normalize_mcp_slash` HTTP-middleware slot in `main.py`.

Three things ship together:

1. **Authentication** — a login page + signed JWT sessions, credentials checked
   against a users table in SQLite, behind a pluggable `AuthProvider` so
   OIDC/SSO can slot in later.
2. **Authorization** — two server-enforced account roles, `owner` (Data Product
   Owner) and `engineer`, enforcing the PO↔Engineer boundary.
3. **Read-only mode** — a single global flag that blocks every mutation for
   everyone (REST, WebSocket, MCP) and shows a banner. This is the primary
   safeguard for a hosted/shared demo instance.

Everything is **gated**: with `WB_AUTH_SECRET` unset, auth is disabled and local
dev is unchanged (a default dev `engineer` identity, no `/login` redirect).

---

## Environment variables

| Var | Default | Effect |
|---|---|---|
| `WB_AUTH_SECRET` | *(unset)* | HS256 signing secret. **Setting it enables auth.** Use a long random value on any shared deploy. |
| `WB_AUTH_REQUIRE` | `0` | Fail-closed opt-in: when `1`, auth is required even if the secret is empty (which then raises rather than signing with a guessable key). |
| `WB_AUTH_TOKEN_TTL_HOURS` | `12` | Session-token lifetime. |
| `WB_READ_ONLY` | `0` | Global read-only mode. Read **live** — flip it without a restart. |
| `WB_CORS_ORIGINS` | *(unset)* | Comma-separated extra CORS origins (in addition to localhost + `WB_FRONTEND_URL`). |
| `WB_FRONTEND_URL` | `http://localhost:5173` | Always added to the CORS allow-list. |
| `VITE_API_BASE` | `http://localhost:8000` | Frontend build-time backend origin (hosted deploys). |

MCP tokens (`WB_MCP_TOKENS`) are separate and role-bindable — see
[`mcp-architecture.md`](./mcp-architecture.md#token-format-role-bound-4-field).

---

## Seeding accounts

Accounts live in the `AppUser` SQLite table (pbkdf2-hashed passwords, never
plaintext). Seed them out-of-band with `scripts/seed_users.py` (run from the
repo root so the `workbench.backend.*` imports resolve):

```bash
env/bin/python scripts/seed_users.py \
    --email po@example.com  --name "Pat Owner"     --role owner    --password 'change-me'
env/bin/python scripts/seed_users.py \
    --email eng@example.com --name "Erin Engineer" --role engineer --password 'change-me'
```

Re-running with the same `--email` updates the account (name / role / password /
active). `--inactive` disables an account (login refused).

Compose secrets (`WB_AUTH_SECRET`, `WB_READ_ONLY`, and role-bound
`WB_MCP_TOKENS`) belong in the gitignored `.env` — shell exports don't survive
across container `up`.

---

## Roles

Two **account roles** are enforced server-side:

- **`owner`** (Data Product Owner) — the Product Workbench: wizards, My Products,
  Ingest, the source-validation gate, marketplace deploy, Score OSI.
- **`engineer`** — the Engineering Workbench pipeline. An engineer account may
  still assume the engineer-side *hats* (Steward / DQA / Reviewer) **client-side**
  (matching the shell split); the server sees only the `engineer` account role
  and enforces just the PO↔Engineer boundary.

Enforcement lives at the load-bearing mutation vectors (a single choke point per
vector, reused by REST + MCP — no brittle per-endpoint matrix):

- Stage execution — `stage_execution.start_stage_run(..., acting_role=)` and the
  `cli.py` stream, via `authz.role_can_run_stage`.
- Reviews — the `reviews.py` POST handlers, via `Depends(require_review(<type>))`
  (`authz.role_can_review`).
- A few unambiguous routes via `Depends(require_role(...))`: serving +
  materialization = `engineer`; OSI evaluate + ODCS publish = `owner`.

Finer stage-level gating (which engineer hat) stays client-side (`canRunStage`),
now driven by the *authenticated* account role.

---

## Read-only mode (for a demo box)

Set `WB_READ_ONLY=1`. Then, for everyone:

- **REST** — any non-`GET`/`HEAD`/`OPTIONS` `/api/*` call → `403 {read_only:true}`
  (the one exception is `/api/auth/login`, so a user can still sign in).
- **WebSocket** — the mutating actions (`run_stage`, chat `send_message`,
  product-chat `send_message` / `delete_session`) are rejected.
- **MCP** — every write tool refuses via `_deny_write()`; reads still work.
  Read-only MCP tokens (`token:principal:*:viewer`) exist for headless clients.
- **Frontend** — a persistent banner ("Read-only demo instance — actions
  disabled") and disabled primary controls; the server 403 is the real guard.

Because `config.read_only()` is read live, flipping the flag off restores writes
without a restart.

---

## The `AuthProvider` seam (future OIDC/SSO)

`auth.AuthProvider` is an ABC with `authenticate(email, password)` and
`get_user(email)`. Today `StaticUserProvider` validates against `AppUser`. To
add OIDC/SSO later, implement an `OIDCProvider` and install it with
`auth.set_provider(...)` — no call sites change (the router + middleware only
know `AuthProvider`, `issue_token`, and `decode_token`). JWT claims
(`sub`/`name`/`role`/`exp`) are the stable contract the rest of the system reads.

---

## Verifying locally

```bash
# Auth OFF (default): existing flows unchanged, no /login redirect.
uvicorn workbench.backend.main:app --reload    # WB_AUTH_SECRET unset

# Auth ON:
export WB_AUTH_SECRET=$(python -c "import secrets;print(secrets.token_urlsafe(48))")
env/bin/python scripts/seed_users.py --email eng@example.com --role engineer --password pw
curl -s -X POST localhost:8000/api/auth/login -H 'content-type: application/json' \
     -d '{"email":"eng@example.com","password":"pw"}'          # → {token: …}
curl -s localhost:8000/api/settings                             # → 401 (no token)

# Read-only:
export WB_READ_ONLY=1
curl -s -X POST localhost:8000/api/auth/logout                  # → 403 read_only
```
