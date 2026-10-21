"""MCP security invariants.

Two things the parity suite never covers (its `conftest` boots MCP in OPEN mode —
`WB_MCP_ALLOW_INSECURE=1`, no tokens — so the bearer middleware and the fail-closed
`_serving_guard` refuse-path are never exercised):

  1. **Static guard-coverage** — every project-scoped tool must call
     `_deny_project` (a scoped token must not act cross-project); the domain-data
     tools must call `_deny_domain`. This is the class of bug where
     `set_source_binding` shipped with only `_deny_write` and let a scoped token
     rebind an out-of-scope project.
  2. **The auth/refuse paths** — the 401 on a missing/bad bearer, and that
     `_deny_write` now folds in `_serving_guard` so an *un*-scoped write
     (create_connection, the intake-decision tools) still refuses in the
     fail-closed "no WB_MCP_TOKENS configured" state.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

from workbench.backend import mcp_server

REPO = Path(__file__).resolve().parents[3]
DE = REPO / "workbench" / "backend" / "mcp_server.py"
PO = REPO / "workbench" / "backend" / "po_mcp_server.py"


def _tool_funcs(path: Path, decorator: str):
    """Yield (FunctionDef, source) for every @<decorator>.tool() function."""
    src = path.read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
            isinstance(d, ast.Call)
            and getattr(d.func, "attr", None) == "tool"
            and getattr(getattr(d.func, "value", None), "id", None) == decorator
            for d in node.decorator_list
        ):
            yield node, (ast.get_source_segment(src, node) or "")


# Project-scoped tools that scope via DELEGATION to another guarded tool, so they
# legitimately don't call _deny_project directly. Keep this list tiny + documented.
_DELEGATES_SCOPE = {
    "get_plan_summary",  # → get_project_state, which calls _deny_project
}

# Domain-scoped tools that execute over a domain's DATA (not marketplace-global
# reads / project creation / stateless suggestion helpers) → must _deny_domain.
_DOMAIN_DATA_TOOLS = {
    "query_semantic_layer",
    "get_semantic_discovery_status",
    "run_semantic_discovery_step",
    "reset_semantic_discovery",
}


def test_project_code_tools_enforce_deny_project():
    """Every @mcp.tool()/@po_mcp.tool() taking a `project_code` parameter must call
    `_deny_project(project_code)` (except documented delegators). Guards against a
    scoped token acting on a project outside its scope."""
    offenders: list[str] = []
    for path, dec in ((DE, "mcp"), (PO, "po_mcp")):
        for node, body in _tool_funcs(path, dec):
            params = [a.arg for a in node.args.args]
            if "project_code" not in params:
                continue
            if node.name in _DELEGATES_SCOPE:
                continue
            if "_deny_project(" not in body:
                offenders.append(f"{path.name}::{node.name}")
    assert not offenders, (
        "project-scoped MCP tools missing _deny_project(project_code) — a scoped "
        "token could act cross-project (add the guard, or document a delegator in "
        "_DELEGATES_SCOPE):\n  " + "\n  ".join(offenders)
    )


def test_domain_data_tools_enforce_deny_domain():
    """The domain-scoped tools that read/execute over a domain's deployed data must
    call `_deny_domain(domain)` — otherwise a project-scoped token reads data from
    domains it isn't authorized for (the query_semantic_layer gap)."""
    bodies = {node.name: body for node, body in _tool_funcs(DE, "mcp")}
    offenders = [
        n for n in _DOMAIN_DATA_TOOLS if n in bodies and "_deny_domain(" not in bodies[n]
    ]
    assert not offenders, (
        "domain-data MCP tools missing _deny_domain(domain):\n  " + "\n  ".join(offenders)
    )


def test_deny_write_refuses_when_serving_unconfigured(monkeypatch):
    """P3 fix: _deny_write folds in _serving_guard, so an un-scoped write still
    refuses in the fail-closed 'no WB_MCP_TOKENS configured' state (the state
    where the bearer middleware also passes through with no token)."""
    monkeypatch.setattr(mcp_server, "_SERVING_BLOCKED", True)
    err = mcp_server._deny_write()
    assert err is not None and "not accepting requests" in err["error"]


def test_deny_write_allows_when_serving_configured(monkeypatch):
    """With serving configured and no read-only/viewer context, _deny_write passes."""
    monkeypatch.setattr(mcp_server, "_SERVING_BLOCKED", False)
    monkeypatch.setattr(mcp_server.config, "read_only", lambda: False)
    token = mcp_server._auth_ctx.set(None)
    try:
        assert mcp_server._deny_write() is None
    finally:
        mcp_server._auth_ctx.reset(token)


def _drive_middleware(headers: dict[str, str], path: str = "/mcp/"):
    """Run _BearerAuthMiddleware once against a fake ASGI HTTP call.
    Returns (response_status, downstream_was_called)."""
    called = {"v": False}

    async def app(scope, receive, send):
        called["v"] = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw = mcp_server._BearerAuthMiddleware(app)
    sent: list[dict] = []

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http",
        "path": path,
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    asyncio.run(mw(scope, receive, send))
    status = next(
        (m["status"] for m in sent if m.get("type") == "http.response.start"), None
    )
    return status, called["v"]


def test_bearer_middleware_401_on_missing_or_bad_token(monkeypatch):
    """With tokens configured, a missing or unknown bearer gets a 401 and never
    reaches the downstream MCP app; a valid bearer passes through."""
    monkeypatch.setattr(mcp_server, "_MCP_TOKENS", mcp_server._parse_tokens("good:eng"))

    status, called = _drive_middleware({})  # no Authorization header
    assert status == 401 and called is False

    status, called = _drive_middleware({"Authorization": "Bearer nope"})
    assert status == 401 and called is False

    status, called = _drive_middleware({"Authorization": "Bearer good"})
    assert status == 200 and called is True


def test_bearer_middleware_open_when_no_tokens(monkeypatch):
    """Fail-open transport only when NO tokens are configured (dev). The tool-level
    _serving_guard is what fails closed in that state — see _deny_write test above."""
    monkeypatch.setattr(mcp_server, "_MCP_TOKENS", {})
    status, called = _drive_middleware({})
    assert status == 200 and called is True
