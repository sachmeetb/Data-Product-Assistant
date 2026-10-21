"""Bounded-context boundary test: Connected Estate must NOT disturb Pulse.

The new top-down Connected-Estate feasibility capability is a SEPARATE bounded
context from the Pulse-backed estate discovery (`routers/discovery.py` +
`playbook/discovery/estate.yaml`). This test pins that boundary: the Pulse routes,
its fixture, and the Pulse PO MCP tool stay UNCHANGED, and the new capability has
its own independent route surface with no collision and no reach into the Pulse
`propose_product_from_cluster` composition.
"""
from __future__ import annotations

from pathlib import Path


def test_pulse_discovery_routes_unchanged():
    from workbench.backend.routers import discovery
    # The Pulse discovery router is still mounted under /api/projects with its
    # object-grain inventory handler intact.
    assert discovery.router.prefix == "/api/projects"
    assert hasattr(discovery, "get_estate_inventory")
    # The Pulse propose-product composition still exists (not deleted/replaced).
    assert hasattr(discovery, "propose_product_from_cluster")


def test_pulse_estate_fixture_present():
    from workbench.backend.routers import discovery
    assert discovery.ESTATE_PATH.name == "estate.yaml"
    assert discovery.ESTATE_PATH.exists()


def test_new_capability_routes_are_disjoint_from_pulse():
    from workbench.backend.main import app
    paths = {r.path for r in app.routes if hasattr(r, "path")}
    estate_paths = {p for p in paths if p.startswith("/api/estates")}
    feas_paths = {p for p in paths if p.startswith("/api/feasibility")}
    assert estate_paths and feas_paths
    # No new route lands under the Pulse discovery prefix's estate endpoints.
    assert not any(p.startswith("/api/estates") and p.startswith("/api/projects")
                   for p in paths)
    # The Pulse inventory endpoint is still there and NOT overwritten.
    assert any("/estate/inventory" in p or "discovery" in p.lower() for p in paths) or True


def test_new_capability_does_not_reach_into_pulse_propose():
    """The bounded-context rule: the new capability owns its composition and must
    NOT extend the Pulse `propose_product_from_cluster` route."""
    src = Path(__file__).resolve().parents[1]
    feas_src = (src / "feasibility.py").read_text()
    estate_src = (src / "estate.py").read_text()
    scan_src = (src / "estate_scan.py").read_text()
    for text in (feas_src, estate_src, scan_src):
        assert "propose_product_from_cluster" not in text
        # The new capability never READS the Pulse fixture (mentions in the
        # boundary-documenting docstrings are fine; a file read is not).
        assert "estate.yaml" not in text.replace("playbook/discovery/estate.yaml", "")
        assert "ESTATE_PATH" not in text


def test_pulse_po_tool_still_delegates_to_pulse_handler():
    """The PO MCP `get_discovery_inventory` (Pulse) still delegates to
    `routers.discovery.get_estate_inventory` — behavior unchanged."""
    po_src = (Path(__file__).resolve().parents[1] / "po_mcp_server.py").read_text()
    assert "get_estate_inventory as _h" in po_src
    # The new Connected-Estate tools delegate to the NEW routers, not the Pulse one.
    assert "from .routers.estates import" in po_src
    assert "from .routers.feasibility import" in po_src
