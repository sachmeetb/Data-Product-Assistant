"""Blueprint Library ⇄ feasibility corpus bootstrap + regeneration.

Two importable cores (thin script wrappers live under ``scripts/``):

- :func:`seed_template_library` — load the committed ODCS seed files
  (``playbook/template_seed/**/*.yaml``) into an empty Library as read-only
  ``origin=seed``, ``status=published`` templates. Idempotent: a target id is
  force-cleared then re-created, so a re-run is a clean no-churn reload that also
  picks up edits to the committed seed.
- :func:`regenerate_feasibility_corpus` — enumerate PUBLISHED templates, project
  each to a ``FeasibilitySpec`` (ODCS→C via :mod:`feasibility_map`), validate
  every one fail-closed with ``feasibility_spec.parse_spec`` BEFORE touching the
  filesystem, then rewrite ``reference/<domain>/<spec>.yaml`` + ``corpus.yaml``
  (bumped ``corpus_version``) + ``index.yaml``.
- :func:`load_specs_from_graph` / :func:`load_index_from_graph` /
  :func:`list_domains_from_graph` — the **runtime** loaders. Feasibility no
  longer reads the baked corpus files: the scanner / list / index read the
  published templates **live from the graph** (the evaluator LLM never touches
  the corpus — it runs tool-less over a backend-built bundle — so every consumer
  is backend Python that already holds a DB ``session``). A newly-published
  template therefore appears in Feasibility immediately and survives rebuilds.

The baked ``reference/`` corpus + :func:`regenerate_feasibility_corpus` now serve
only a **fresh-instance seed** (see :func:`bootstrap_template_library`) + the
offline/test path; publish/retract no longer regenerate them.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml
from sqlmodel import Session

from . import config
from . import feasibility_map as fm
from . import feasibility_spec as fs
from .estate import estate_graph_session
from .template_store import (
    DELETE_TEMPLATE,
    canonicalize_template,
    list_templates_raw,
    _read_template_from_graph,
    _save_template_to_graph,
)

# Committed ODCS seed files — the hand-maintained source of truth for the seed
# templates (reviewed + version-controlled).
TEMPLATE_SEED_DIR = config.BASE_DIR / "playbook" / "template_seed"

SEED_OWNER = "system"


# ── seed bootstrap ────────────────────────────────────────────────────────────

def _iter_seed_files(seed_dir: Path):
    if not seed_dir.is_dir():
        return
    for path in sorted(seed_dir.rglob("*.yaml")):
        if path.is_file():
            yield path


def seed_template_library(
    session: Session, *, seed_dir: Optional[Path] = None
) -> dict[str, Any]:
    """Load committed ODCS seed files into the Library (idempotent)."""
    seed_dir = seed_dir or TEMPLATE_SEED_DIR
    loaded: list[str] = []
    errors: list[dict[str, str]] = []
    for path in _iter_seed_files(seed_dir):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(raw, dict) or not raw.get("id"):
                raise ValueError("seed file must be an ODCS object carrying an id")
            spec = canonicalize_template(raw)
            contract_id = spec["id"]
            # Force-clear the target id first so a re-run never accumulates
            # versions and always reflects the committed seed exactly.
            with estate_graph_session(session) as ns:
                ns.run(DELETE_TEMPLATE, contract_id=contract_id)
            _save_template_to_graph(
                spec,
                session,
                owner_email=SEED_OWNER,
                status="published",
                origin="seed",
                change_kind="auto",
                revision_notes="seed bootstrap",
            )
            loaded.append(contract_id)
        except Exception as e:  # noqa: BLE001 — collect + report, don't abort the batch
            errors.append({"file": path.name, "error": str(e)})
    return {"loaded": loaded, "count": len(loaded), "errors": errors,
            "seed_dir": str(seed_dir)}


# ── Library → feasibility corpus regeneration ──────────────────────────────────

def _bump_version(current: str) -> str:
    """Monotonic minor bump: ``1.0`` → ``1.1``; ``0`` → ``0.1``; else ``<v>.1``."""
    cur = (current or "0").strip()
    if "." in cur:
        major, _, minor = cur.partition(".")
        try:
            return f"{major}.{int(minor) + 1}"
        except ValueError:
            return f"{cur}.1"
    try:
        return f"{int(cur)}.1"
    except ValueError:
        return f"{cur}.1"


def _published_templates(session: Session) -> list[dict]:
    return [r for r in list_templates_raw(session) if r.get("status") == "published"]


def regenerate_feasibility_corpus(
    session: Session, *, version: Optional[str] = None
) -> dict[str, Any]:
    """Rewrite the baked feasibility corpus from every PUBLISHED template."""
    rows = _published_templates(session)

    # Build + validate every candidate BEFORE touching the filesystem.
    validated: list[dict[str, Any]] = []      # raw FeasibilitySpec dicts
    skipped: list[dict[str, str]] = []
    domains: set[str] = set()
    for row in rows:
        contract_id = row["id"]
        odcs = _read_template_from_graph(contract_id, session)
        if not odcs:
            skipped.append({"id": contract_id, "reason": "unreadable"})
            continue
        raw = fm.odcs_to_feasibility_spec(odcs)
        # A template with no columns can't be a gradeable feasibility spec.
        if not raw.get("attributes"):
            skipped.append({"id": contract_id, "reason": "no attributes"})
            continue
        # Fail-closed: a shaped-but-invalid spec is a real bug → propagate.
        fs.parse_spec(raw)
        validated.append(raw)
        domains.add(raw["domain"])

    new_version = version or _bump_version(fs.corpus_version())

    corpus_dir = fs.CORPUS_DIR
    corpus_dir.mkdir(parents=True, exist_ok=True)
    # The generator OWNS the corpus now — clear prior domain dirs + manifest +
    # index, keeping any sibling docs untouched.
    for child in corpus_dir.iterdir():
        if child.is_dir():
            import shutil
            shutil.rmtree(child)
    fs.CORPUS_MANIFEST.unlink(missing_ok=True)
    fs.INDEX_FILE.unlink(missing_ok=True)

    index_specs: list[dict[str, Any]] = []
    written: list[str] = []
    for raw in validated:
        domain_dir = corpus_dir / fs.slug(raw["domain"], "misc")
        domain_dir.mkdir(parents=True, exist_ok=True)
        spec_file = domain_dir / f"{fs.slug(raw['spec_id'], 'spec')}.yaml"
        spec_file.write_text(
            yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
        written.append(str(spec_file.relative_to(corpus_dir)))
        req = [a for a in raw["attributes"] if a.get("required", True)]
        index_specs.append({
            "spec_id": raw["spec_id"],
            "name": raw["name"],
            "domain": raw["domain"],
            "product_kind": raw.get("product_kind", "consumer"),
            "description": raw.get("description", ""),
            "required_count": len(req),
            "total_count": len(raw["attributes"]),
        })

    manifest = {
        "corpus_version": new_version,
        "generated_from": "blueprint-library",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "spec_count": len(written),
        "domains": sorted(domains),
    }
    fs.CORPUS_MANIFEST.write_text(
        yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    fs.INDEX_FILE.write_text(
        yaml.safe_dump({"specs": index_specs}, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    return {
        "corpus_version": new_version,
        "written": written,
        "written_count": len(written),
        "skipped": skipped,
        "domains": sorted(domains),
    }


# ── runtime graph-backed spec loaders (retire the file corpus at runtime) ──────

# The cheap live stamp that replaces the file ``corpus_version``: the evaluator
# never reads the corpus files (it runs tool-less over a backend-built bundle),
# so runtime reads the PUBLISHED Blueprint Library straight from the graph and a
# run just records that it did (the user doesn't need the reproducibility
# snapshot the file cache used to provide).
GRAPH_CORPUS_MARKER = "graph"


def load_specs_from_graph(
    session: Session, domain: Optional[str] = None
) -> list[fs.FeasibilitySpec]:
    """Load every PUBLISHED template as a validated ``FeasibilitySpec``, live from
    the graph — the runtime replacement for ``feasibility_spec.load_corpus``.

    Mirrors :func:`regenerate_feasibility_corpus`'s projection loop **minus the
    file write**, but **skips on error instead of failing closed**: a single
    malformed / attribute-less template must not break the whole list or scan."""
    want = fs.slug(domain) if domain else None
    specs: list[fs.FeasibilitySpec] = []
    for row in _published_templates(session):
        # Cheap head-row domain pre-filter — avoid a full read for a template we'd
        # discard anyway (spec.domain below is the authoritative filter).
        head_domain = (row.get("domain") or "").strip()
        if want and head_domain and fs.slug(head_domain) != want:
            continue
        odcs = _read_template_from_graph(row["id"], session)
        if not odcs:
            continue
        raw = fm.odcs_to_feasibility_spec(odcs)
        if not raw.get("attributes"):
            continue
        try:
            spec = fs.parse_spec(raw)
        except fs.FeasibilitySpecValidationError:
            continue
        if want and fs.slug(spec.domain) != want:
            continue
        spec.corpus_version = GRAPH_CORPUS_MARKER
        specs.append(spec)
    return specs


def load_index_from_graph(
    session: Session, domain: Optional[str] = None
) -> list[dict[str, Any]]:
    """Project the published templates to the lightweight index shape the UI spec
    picker consumes (mirrors ``feasibility_spec.load_index``). One full read per
    published template — fine for ~50 specs; a single-query count is an easy later
    optimization if it turns sluggish."""
    return [
        {
            "spec_id": s.spec_id, "name": s.name, "domain": s.domain,
            "product_kind": s.product_kind.value, "description": s.description,
            "required_count": len(s.required_attributes),
            "total_count": len(s.attributes),
        }
        for s in load_specs_from_graph(session, domain)
    ]


def list_domains_from_graph(session: Session) -> list[str]:
    """Sorted distinct domains across the published templates."""
    return sorted({s.domain for s in load_specs_from_graph(session)})


def bootstrap_template_library(session: Session, *, seed_dir: Optional[Path] = None) -> dict:
    """Fresh-instance bootstrap: seed the Library then regenerate the corpus."""
    seed_result = seed_template_library(session, seed_dir=seed_dir)
    corpus_result = regenerate_feasibility_corpus(session)
    return {"seed": seed_result, "corpus": corpus_result}


_COUNT_SEED_TEMPLATES = "MATCH (dc:ProductTemplate {templateOrigin: 'seed'}) RETURN count(dc) AS c"


def seed_if_empty(session: Session, *, seed_dir: Optional[Path] = None) -> dict:
    """Seed the Library + regenerate the corpus ONLY when no seed templates
    exist yet (a fresh instance). Idempotent + cheap on an already-seeded
    instance (a single count query, then skip). Called at app startup."""
    with estate_graph_session(session) as ns:
        row = ns.run(_COUNT_SEED_TEMPLATES).single()
        existing = int(row["c"]) if row else 0
    if existing > 0:
        return {"seeded": False, "existing_seeds": existing}
    return {"seeded": True, **bootstrap_template_library(session, seed_dir=seed_dir)}
