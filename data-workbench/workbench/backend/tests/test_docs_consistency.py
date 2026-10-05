"""Docs-drift guard — keeps the prose honest about the code.

Every finding here was a REAL defect found by an independent review, twice over
for the MCP count. These are cheap filesystem asserts (no Neo4j, no SQLite, no
backend import) that fail the suite the moment the docs and the code disagree
again:

  - the MCP tool count quoted in docs vs the live `@mcp.tool()` decorator count;
  - retired graph-schema terms (renamed properties / relationships) resurfacing;
  - references to directories that no longer exist;
  - the wrong Claude SDK distribution creeping back into a requirements file
    (the backend imports `claude_agent_sdk`; installing `claude-code-sdk`
    instead leaves a clean venv unable to import the backend at all).

Extending: add to RETIRED_TERMS when you rename a graph property/relationship,
so the rename can't silently rot the docs.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
DOCS = REPO / "docs"
BACKEND = REPO / "workbench" / "backend"
MCP_SERVER = BACKEND / "mcp_server.py"
PO_MCP_SERVER = BACKEND / "po_mcp_server.py"

# Diagram sources are generated-artifact inputs (label strings baked into SVG /
# excalidraw output), not prose or runtime code — they don't get linted.
_EXCLUDED_DIRS = {"diagrams", "node_modules", "__pycache__"}

# Dated audit / snapshot docs record what was true on a given day (e.g.
# "mcp-architecture.md says there are 22 tools"). Rewriting them to match today
# would destroy the record, so they're exempt from currency checks.
_DATED_DOC = re.compile(r"\d{4}-\d{2}-\d{2}")

_SELF = Path(__file__).name


def _markdown_files(root: Path) -> list[Path]:
    return [
        p for p in root.rglob("*.md")
        if not any(part in _EXCLUDED_DIRS for part in p.parts)
        and not _DATED_DOC.search(p.name)
    ]


def _backend_py_files() -> list[Path]:
    return [
        p for p in BACKEND.rglob("*.py")
        if "__pycache__" not in p.parts and p.name != _SELF
    ]


def _live_mcp_tool_count() -> int:
    return len(re.findall(r"^@mcp\.tool\(\)", MCP_SERVER.read_text(), re.M))


def _live_po_mcp_tool_count() -> int:
    # 0 when the PO server isn't present (e.g. mid-merge, before po_mcp_server.py
    # lands). A 0 count means "no PO surface to check" — PO count claims are then
    # skipped rather than asserted against a phantom count.
    if not PO_MCP_SERVER.exists():
        return 0
    return len(re.findall(r"^@po_mcp\.tool\(\)", PO_MCP_SERVER.read_text(), re.M))


# A doc line talking about the PO front door (`/po-mcp`, `po_mcp_server.py`,
# `@po_mcp.tool()`, "PO MCP server") states the PO tool count, not the DE one.
_PO_LINE = re.compile(r"po[-_]?mcp|product\s+owner\s+(?:mcp|endpoint|server)")


def test_mcp_tool_count_matches_docs():
    """Any doc quoting "<N> MCP tools" / "<N> tools" must quote the live count.

    engineer-guide.md and architecture.md both said 24 while the server exposed
    51 — engineers reading them missed ~27 tools (workflow controls, the
    materialization gate, joins/preflight, OKF export, semantic discovery…).

    Two servers now: `/mcp` (DE, `mcp_server.py`) and `/po-mcp` (PO,
    `po_mcp_server.py`). A count claim on a PO-referencing line is checked against
    the PO count; every other mcp-referencing line against the DE count.
    """
    live = _live_mcp_tool_count()
    assert live > 0, "no @mcp.tool() decorators found — did mcp_server.py move?"
    po_live = _live_po_mcp_tool_count()

    # "51 MCP tools", "the 51 tools", "**51** MCP tools", "(51 tools, with …)".
    # Emphasis markers are stripped first — `**51** MCP tools` must not slip
    # through on a `\d+\s+` boundary (it did, on the first cut of this guard).
    claim = re.compile(r"(\d+)\s+(?:MCP\s+)?tools\b")
    offenders: list[str] = []
    for md in _markdown_files(DOCS):
        for line_no, raw in enumerate(md.read_text().splitlines(), 1):
            line = raw.replace("*", "").replace("`", "")
            # Only treat it as a count claim when the line is talking about
            # THE tool surface — not e.g. "3 tools you reach for often".
            if "mcp" not in line.lower() and "mcp" not in md.name.lower():
                continue
            is_po_line = bool(_PO_LINE.search(line.lower()))
            # PO count claims are only enforced once the PO server is present.
            if is_po_line and po_live == 0:
                continue
            expected = po_live if is_po_line else live
            for m in claim.finditer(line):
                n = int(m.group(1))
                if n != expected:
                    offenders.append(
                        f"{md.relative_to(REPO)}:{line_no} claims {n} tools "
                        f"(live count is {expected}): {raw.strip()[:90]}"
                    )
    assert not offenders, (
        "MCP tool count drifted between code and docs:\n  "
        + "\n  ".join(offenders)
    )


# Surface docs OUTSIDE docs/ that also quote the MCP tool counts and drifted
# independently — the kit docs a real engineer/PO reads at onboarding, plus the
# root + backend CLAUDE.md and README. The docs/*.md guard above missed every one
# of these (they live outside docs/, or the count sat on a non-"mcp" line like
# engineer-guide.md's stale "51 tools").
_SURFACE_FILES = [
    "README.md",
    "CLAUDE.md",
    "workbench/backend/CLAUDE.md",
    "engineer-kit/README.md",
    "engineer-kit/AGENTS.md",
    "engineer-kit/skills/workbench-guide/reference.md",
    "po-kit/README.md",
    "po-kit/AGENTS.md",
]
_SURFACE_LINE = re.compile(
    r"mcp|/po-mcp|tool surface|po surface|full surface|@(?:mcp|po_mcp)\.tool", re.I
)
# "<N> tools" / "<N> DE tools" / "<N> PO MCP tools" — emphasis/backticks stripped first.
_TOOLS_COUNT = re.compile(r"(\d+)\s+(?:DE\s+|PO\s+)?(?:MCP\s+)?tools\b")


def test_mcp_tool_counts_in_surface_docs_are_current():
    """Kit docs / READMEs / CLAUDE.md quote the MCP tool counts too, and drifted
    independently (the docs/*.md guard only scans docs/ on "mcp" lines). Any
    "<N> ... tools" claim on a tool-surface line in these files must equal the
    live DE or PO count — robust to a single line quoting BOTH (both are allowed
    values), which is why we don't try to classify each number as DE-vs-PO."""
    de = _live_mcp_tool_count()
    po = _live_po_mcp_tool_count()
    allowed = {de, po} if po else {de}
    offenders: list[str] = []
    for rel in _SURFACE_FILES:
        path = REPO / rel
        if not path.exists():
            continue
        for line_no, raw in enumerate(path.read_text().splitlines(), 1):
            line = raw.replace("*", "").replace("`", "")
            if not _SURFACE_LINE.search(line):
                continue
            for m in _TOOLS_COUNT.finditer(line):
                # Not a whole-surface count: a delta ("+11 DE tools") or a subset
                # enumeration ("5 MCP tools (configure_migration, …)").
                if line[: m.start()].rstrip().endswith("+"):
                    continue
                if line[m.end() :].lstrip().startswith("("):
                    continue
                n = int(m.group(1))
                if n not in allowed:
                    offenders.append(
                        f"{rel}:{line_no} claims {n} tools "
                        f"(live: DE={de}, PO={po}): {raw.strip()[:90]}"
                    )
    assert not offenders, (
        "MCP tool count drifted in a surface doc (kit / README / CLAUDE):\n  "
        + "\n  ".join(offenders)
    )


def _tool_names(path: Path, decorator: str) -> list[str]:
    src = path.read_text().splitlines()
    out: list[str] = []
    for i, line in enumerate(src):
        if re.match(rf"^@{decorator}\.tool\(\)", line):
            for j in range(i + 1, min(i + 6, len(src))):
                m = re.search(r"def\s+([a-zA-Z_]\w*)", src[j])
                if m:
                    out.append(m.group(1))
                    break
    return out


def test_every_mcp_tool_is_documented_in_reference():
    """docs/mcp-architecture.md calls itself THE canonical tool reference — so
    every live @mcp.tool()/@po_mcp.tool() must actually appear in it. This is the
    guard the count-only check missed: the reference read "137 tools" while its
    tables silently omitted 38 (the entire dmig + cmig families). A new tool that
    isn't documented here fails the suite."""
    ref = (DOCS / "mcp-architecture.md").read_text()

    def _documented(name: str) -> bool:
        # `name` (backtick-quoted, the table convention) or a bare word-boundary hit.
        return (
            f"`{name}`" in ref
            or re.search(rf"(?<![\w]){re.escape(name)}(?![\w])", ref) is not None
        )

    missing = [f"/mcp:{n}" for n in _tool_names(MCP_SERVER, "mcp") if not _documented(n)]
    if PO_MCP_SERVER.exists():
        missing += [
            f"/po-mcp:{n}" for n in _tool_names(PO_MCP_SERVER, "po_mcp") if not _documented(n)
        ]
    assert not missing, (
        "MCP tools missing from the canonical reference docs/mcp-architecture.md "
        "(add them there — the count matching is not enough):\n  "
        + "\n  ".join(sorted(missing))
    )


# Relationships/labels that were renamed with NO back-compat use anywhere — a
# hard ban in both docs and code.
RETIRED_HARD = {
    "ON_PRODUCT_COLUMN": "ON_DPROD_COLUMN",
}

# Properties that were superseded but whose OLD name still legitimately appears
# in migration/back-compat code (`graph_ops.ensure_contract_versioning` backfills
# it; `summary.py` coalesces over it) and in docs that explain the migration.
# So these are banned only when a line presents them as CURRENT — a line that
# marks them as historical is fine.
RETIRED_SOFT = {
    "lifecycleVersion": "currentVersion / currentLifecycleState",
}
_LEGACY_MARKERS = (
    "old", "legacy", "no longer", "retired", "deprecat", "migrat",
    "backfill", "coalesce", "back-compat", "was ", "formerly", "gone",
)


def test_no_retired_graph_relationships():
    """A renamed relationship with no back-compat use must not survive anywhere.

    `ON_PRODUCT_COLUMN` was renamed to `ON_DPROD_COLUMN`; the code uses only the
    new name, but architecture.md's graph diagram still showed the old one.
    """
    offenders: list[str] = []
    for path in _markdown_files(DOCS) + _backend_py_files():
        text = path.read_text()
        for retired, replacement in RETIRED_HARD.items():
            if retired in text:
                offenders.append(
                    f"{path.relative_to(REPO)} still uses `{retired}` (now: {replacement})"
                )
    assert not offenders, "retired graph terms found:\n  " + "\n  ".join(offenders)


def test_retired_properties_not_presented_as_current():
    """Docs may EXPLAIN a superseded property, but must not present it as the
    live model. architecture.md's diagram listed `lifecycleVersion` on
    :DataContract as if it were current; marketplace.md's mention ("the old
    lifecycleVersion pin is gone") is legitimate and must keep passing."""
    offenders: list[str] = []
    for md in _markdown_files(DOCS):
        for line_no, line in enumerate(md.read_text().splitlines(), 1):
            for retired, replacement in RETIRED_SOFT.items():
                if retired not in line:
                    continue
                if any(marker in line.lower() for marker in _LEGACY_MARKERS):
                    continue  # explicitly framed as historical — fine
                offenders.append(
                    f"{md.relative_to(REPO)}:{line_no} presents `{retired}` as current "
                    f"(now: {replacement}): {line.strip()[:80]}"
                )
    assert not offenders, (
        "superseded properties presented as current:\n  " + "\n  ".join(offenders)
    )


def test_no_references_to_deleted_notes_dir():
    """`notes/` was renamed to `research/`. Stale paths leaked into the
    generated per-project CLAUDE.md via the jinja template."""
    assert not (REPO / "notes").exists(), (
        "notes/ exists again — either restore it in the docs or keep using research/"
    )
    targets = [
        p for p in _markdown_files(DOCS)
        # research/ is scratch and the audit snapshots are historical; the
        # currency rule is for docs that describe the system TODAY.
        if "research" not in p.parts
    ] + list((BACKEND / "templates").rglob("*.j2"))
    offenders = [
        f"{p.relative_to(REPO)}"
        for p in targets
        if re.search(r"(?<![\w/])notes/", p.read_text())
    ]
    assert not offenders, (
        "references to the deleted notes/ dir (use research/):\n  "
        + "\n  ".join(offenders)
    )


def test_requirements_pin_the_agent_sdk_not_the_code_sdk():
    """The backend imports `claude_agent_sdk`. A requirements file that installs
    `claude-code-sdk` instead produces a venv that cannot import the backend —
    exactly the clean-setup break the review caught."""
    req_files = [REPO / "requirements.txt", BACKEND / "requirements.txt"]
    for req in req_files:
        if not req.exists():
            continue
        pins = [
            ln.strip() for ln in req.read_text().splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        assert not any(p.startswith("claude-code-sdk") for p in pins), (
            f"{req.relative_to(REPO)} pins claude-code-sdk — the backend imports "
            f"claude_agent_sdk; installing the wrong distribution breaks a clean venv"
        )
        assert any(p.startswith("claude-agent-sdk") for p in pins), (
            f"{req.relative_to(REPO)} does not pin claude-agent-sdk, which every "
            f"backend SDK call site imports"
        )


def test_backend_and_root_requirements_agree_on_shared_pins():
    """The backend's requirements must not contradict the root's for packages
    both pin — the container builds from root, so a divergent backend pin means
    "works in Docker, broken in a local venv". This is how fastapi 0.115 /
    uvicorn 0.30 survived in the backend file long after the container moved to
    0.135 / 0.42 — and those stale pins could not even resolve against
    claude-agent-sdk's `mcp` dependency."""
    def _pins(path: Path) -> dict[str, str]:
        out: dict[str, str] = {}
        for ln in path.read_text().splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#") or "==" not in ln:
                continue
            name, _, ver = ln.partition("==")
            # normalise `uvicorn[standard]` → `uvicorn`
            out[name.split("[")[0].strip().lower()] = ver.strip()
        return out

    root = _pins(REPO / "requirements.txt")
    backend = _pins(BACKEND / "requirements.txt")
    conflicts = [
        f"{pkg}: backend pins {ver}, root pins {root[pkg]}"
        for pkg, ver in backend.items()
        if pkg in root and root[pkg] != ver
    ]
    assert not conflicts, (
        "backend and root requirements pin different versions:\n  "
        + "\n  ".join(conflicts)
    )


def test_backend_imports_only_the_agent_sdk():
    """No runtime module may import the retired SDK package."""
    offenders = [
        f"{p.relative_to(REPO)}"
        for p in _backend_py_files()
        if "claude_code_sdk" in p.read_text()
    ]
    assert not offenders, (
        "backend modules importing/mentioning claude_code_sdk (use claude_agent_sdk):\n  "
        + "\n  ".join(offenders)
    )


# ── data-mapping advisor kind-list drift guard ───────────────────────────────
# The LLM-facing SKILL.md advertises the transform kinds the mapping advisor may
# author. It MUST cover every kind the writer accepts (write_mappings.py's
# VALID_TRANSFORM_KINDS) — otherwise a newly-added kind (e.g. date_difference) is
# invisible to a fresh run and the advisor falls back to non-portable raw SQL.
_MAPPING_SKILL = REPO / "workbench-skills" / "skills" / "data-mapping-neo4j"


def _writer_valid_kinds() -> set[str]:
    # Load the actual set object (authoritative) rather than regex-parsing the
    # literal — a `}` inside a comment in the block truncates a non-greedy match.
    import importlib.util
    path = _MAPPING_SKILL / "scripts" / "write_mappings.py"
    spec = importlib.util.spec_from_file_location("_wm_kinds_probe", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return set(mod.VALID_TRANSFORM_KINDS)


def _skill_advertised_kinds() -> set[str]:
    """Backtick-quoted kinds on the two SKILL.md list lines: the `transformKind`
    property-table row and the Step-6 `transform_kind` values line."""
    kinds: set[str] = set()
    for line in (_MAPPING_SKILL / "SKILL.md").read_text().splitlines():
        if "`transformKind`" in line or "`transform_kind` values" in line:
            kinds |= set(re.findall(r"`([a-z_]+)`", line))
    return kinds


def test_mapping_skill_kind_lists_cover_writer():
    writer = _writer_valid_kinds()
    advertised = _skill_advertised_kinds()
    assert writer, "writer kind set parsed empty"
    assert advertised, "no SKILL.md kind-list lines found"
    missing = writer - advertised
    assert not missing, (
        "data-mapping-neo4j/SKILL.md kind lists omit writer kinds "
        f"{sorted(missing)} — the advisor can't author them; add to both the "
        "`transformKind` table row and the Step-6 `transform_kind` values line."
    )
