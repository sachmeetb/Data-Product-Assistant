"""Ingest an existing data product from an ODCS v3.1 spec.

Endpoints:

* ``POST /parse-odcs`` — deterministic parse of YAML or JSON content via
  ``_canonicalize_v3_1``. No side effects. Powers the read-only confirmation
  screen.

* ``POST /classify-archetype`` — invokes the ``data-product-archetype-classifier``
  skill via the Claude Code SDK to read the parsed spec and recommend
  ``kind=source|consumer`` with rationale, signals, and (for consumer)
  inferred upstream dependencies. The UI shows this as a recommendation with
  an override toggle.

* ``POST /match-inputs`` — for a consumer-aligned spec, returns per-slot
  marketplace candidates ranked by exact-URI → exact-name → semantic match.
  When no good candidate exists, emits a ``gap_suggestion`` that describes
  what the missing source product should encompass — used to prefill
  ``NewSourceProductWizard`` when the PO clicks "Create now".

* ``POST /drafts`` (upsert) / ``GET /drafts`` (list) / ``GET /drafts/{id}`` /
  ``PATCH /drafts/{id}`` — persist in-flight ingest state so the PO can step
  away to author/import missing source products and return later without
  losing parse, classification, or slot selections.

* ``POST /from-odcs`` — commits the ingest. Accepts ``archetype_override``
  and ``input_selections``. For ``dpe-cf``, scaffolds the consumer default
  workflow templates and gates the request behind all slots being
  ``resolution=='matched'``.
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import re
from datetime import datetime
from typing import Any, Optional

import yaml
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from ..config import BASE_DIR, PIPELINE_PLUGINS
from ..database import get_session
from ..models import (
    IngestDraft,
    Project,
    ProductRequest,
    ProductRequestKind,
    ProductRequestStatus,
)
from ..neo4j_client import neo4j_session
from .._contract_versioning import (
    ALLOWED_UPSTREAM_PRODUCT_KINDS,
    CONSUMABLE_LIFECYCLE_STATES,
    ConsumesBindingError,
)
from .odcs import _canonicalize_v3_1, _save_odcs_to_graph, _generate_dprod
from .projects import ProjectCreate, create_project
from . import marketplace as _marketplace
from . import domain_catalogs as _domain_catalogs


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ingest-products", tags=["ingest-products"])


CLASSIFIER_SKILL = "data-product-archetype-classifier"

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _parse_skill_json(text: str) -> dict[str, Any]:
    """Pull the last fenced JSON block out of an SDK transcript. Tolerates
    extra prose around the block; falls back to a bare ``{}`` payload."""
    matches = _JSON_BLOCK_RE.findall(text or "")
    payload: dict[str, Any] = {}
    if matches:
        try:
            parsed = _json.loads(matches[-1])
            if isinstance(parsed, dict):
                payload = parsed
        except _json.JSONDecodeError:
            pass
    if not payload:
        stripped = (text or "").strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                parsed = _json.loads(stripped)
                if isinstance(parsed, dict):
                    payload = parsed
            except _json.JSONDecodeError:
                pass
    return payload


async def _run_classifier_skill(
    mode: str,
    inputs: dict[str, Any],
) -> tuple[dict[str, Any], Optional[str]]:
    """Invoke the ``data-product-archetype-classifier`` skill in one of its
    three modes: ``classify_archetype``, ``match_inputs``, ``synthesize_missing_source``.

    Returns ``(payload, error)``. On any SDK/import/parse failure, ``error``
    is a short string and ``payload`` is ``{}`` — the caller decides whether
    to fall back to a heuristic or surface the error.
    """
    try:
        from claude_agent_sdk import query, ClaudeAgentOptions
        from claude_agent_sdk.types import (
            AssistantMessage,
            ResultMessage,
            TextBlock,
        )
    except ImportError:
        return {}, "claude-agent-sdk is not installed"

    system_prompt = (
        f"FIRST: Load the `{CLASSIFIER_SKILL}` skill via the Skill tool, then follow its "
        f"instructions for MODE: {mode}. Emit exactly one fenced JSON code block as the "
        "skill instructs. Do not write files. Do not run shell commands. Do not answer "
        "in prose outside the JSON block."
    )

    user_prompt = (
        f"FIRST: Load the {CLASSIFIER_SKILL} skill using the Skill tool.\n\n"
        f"MODE: {mode}\n\n"
        f"INPUTS (JSON):\n{_json.dumps(inputs, ensure_ascii=False)}\n\n"
        "Output the single fenced JSON block as instructed by the skill."
    )

    options = ClaudeAgentOptions(
        allowed_tools=["Read", "Skill"],
        permission_mode="acceptEdits",
        cwd=str(BASE_DIR),
        max_turns=4,
        skills="all",
        plugins=PIPELINE_PLUGINS,
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": system_prompt,
        },
    )

    transcript_parts: list[str] = []
    try:
        async for message in query(prompt=user_prompt, options=options):
            if message is None:
                continue
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        transcript_parts.append(block.text)
            elif isinstance(message, ResultMessage):
                from ..llm_usage import extract_usage, record_usage
                record_usage(source="ingest_classifier", usage=extract_usage(message))
                if message.is_error:
                    return {}, f"Classifier ({mode}) returned an error"
    except Exception as e:
        return {}, f"Classifier ({mode}) failed: {e}"

    payload = _parse_skill_json("\n".join(transcript_parts))
    if not isinstance(payload, dict):
        return {}, f"Classifier ({mode}) output not in the expected shape"
    return payload, None


def _heuristic_classify(spec: dict) -> dict[str, Any]:
    """Cheap deterministic fallback when the classifier skill is unavailable.

    Used so the endpoint never hard-fails — UI always has something to show.
    The real classification is semantic and comes from the skill; this just
    keeps the system responsive in dev or when the SDK isn't installed.
    """
    inputs = spec.get("inputs") or []
    has_inputs = isinstance(inputs, list) and any(
        isinstance(i, dict) and (i.get("dprod_uri") or i.get("dprodUri") or i.get("name"))
        for i in inputs
    )
    text_blob = " ".join(
        str(spec.get(k) or "") for k in ("description", "purpose", "name")
    ).lower()
    consumer_words = ("combine", "join", "blend", "consume", "consumer-aligned", "derived from")
    has_consumer_language = any(w in text_blob for w in consumer_words)
    kind = "consumer" if (has_inputs or has_consumer_language) else "source"
    return {
        "kind": kind,
        "confidence": 60 if (has_inputs or has_consumer_language) else 70,
        "rationale": (
            "Heuristic fallback: detected explicit inputs[] or consumer-aligned language"
            if kind == "consumer"
            else "Heuristic fallback: no inputs[] declared and no consumer-aligned language detected"
        ),
        "signals": (
            (["explicit_inputs_list"] if has_inputs else [])
            + (["consumer_language"] if has_consumer_language else [])
        ),
        "inferred_dependencies": [],
        "_fallback": True,
    }


def _compact_marketplace_row(row: dict) -> dict[str, Any]:
    """Strip a marketplace row to the fields the matcher cares about so the
    skill prompt stays compact. ``product_kind`` rides along so the UI can badge
    each candidate (source / aggregate / consumer) — a consumer may build on any
    published product, so the picker must be able to tell them apart."""
    return {
        "uri": row.get("uri"),
        "contract_id": row.get("contract_id"),
        "name": row.get("name"),
        "domain": row.get("domain"),
        "description": row.get("description"),
        "purpose": row.get("purpose"),
        "product_kind": row.get("product_kind") or "",
    }


def _normalised_name(value: Optional[str]) -> str:
    """Lowercased, whitespace-collapsed, non-alphanum-stripped form used for
    coarse name-match comparisons."""
    if not value:
        return ""
    return re.sub(r"[^a-z0-9]+", "", value.lower())


# Stop-words that don't carry semantic signal in product matching. Kept narrow
# so we don't strip useful domain tokens; this list deliberately excludes
# "sales", "customer", etc.
_SIMILARITY_STOPWORDS = frozenset(
    {
        "a", "an", "the", "and", "or", "of", "for", "to", "from", "with",
        "by", "on", "in", "at", "is", "are", "was", "were", "be", "been",
        "this", "that", "these", "those", "as", "it", "its", "we", "our",
        "all", "any", "data", "product", "products",
    }
)

# Narrower set used by column-level matchers. At product-name granularity,
# tokens like "data" / "product" are noise (every product has "Data Product"
# in its name); at column-name granularity they're domain-meaningful — a
# `product.name` column and a `customer.name` column differ exactly on the
# table-noun token. Keeping them in the column-level bag preserves that
# disambiguating signal.
_COLUMN_LEVEL_STOPWORDS = _SIMILARITY_STOPWORDS - {"data", "product", "products"}


def _tokenise_for_similarity(text: Any, *, stopwords: frozenset[str] = _SIMILARITY_STOPWORDS) -> set[str]:
    """Lowercased word-boundary tokens, minus stop-words. Splits on
    underscores AND camelCase boundaries so column-name tokens like
    ``customer_id`` / ``productName`` decompose into ``customer``, ``id``,
    ``product``, ``name`` rather than collapsing to a single low-overlap
    string. Minimum length 2 — keeps short but meaningful tokens like
    ``id``."""
    if not text:
        return set()
    s = str(text)
    # camelCase → space-separated. Done before lowercasing so the boundary
    # is detectable.
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s)
    tokens = re.findall(r"[a-z][a-z0-9]{1,}", s.lower())
    return {t for t in tokens if t not in stopwords}


def _heuristic_rank_marketplace(
    slot_hint: dict[str, Any],
    pool: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Deterministic keyword-overlap ranker. Used as a fallback when the
    classifier skill is unavailable or returns no ranking.

    Computes a Jaccard-style similarity between the slot hint text (name +
    encompasses) and each marketplace product's (name + description + purpose).
    Returns up to 5 candidates with non-zero overlap, sorted by score
    descending. Score is scaled to 0-100 with mild boosting so a small but
    non-trivial overlap reads as a credible match.
    """
    hint_text = " ".join(
        str(v)
        for v in (
            slot_hint.get("name"),
            slot_hint.get("encompasses"),
        )
        if v
    )
    hint_tokens = _tokenise_for_similarity(hint_text)
    if not hint_tokens:
        return []

    hint_domain = (slot_hint.get("domain") or "").strip().lower() or None

    scored: list[dict[str, Any]] = []
    for p in pool:
        product_text = " ".join(
            str(v)
            for v in (p.get("name"), p.get("description"), p.get("purpose"))
            if v
        )
        product_tokens = _tokenise_for_similarity(product_text)
        if not product_tokens:
            continue
        overlap = hint_tokens & product_tokens
        if not overlap:
            # Even with zero textual overlap, a same-domain candidate is worth
            # surfacing as a weak match — domain alignment alone implies the
            # consumer is plausibly drawing from this product.
            if not hint_domain or (p.get("domain") or "").strip().lower() != hint_domain:
                continue
        overlap_count = len(overlap)
        # Score formula:
        #   • Count-driven base — each shared term reads as a real signal.
        #     1 term tops out below the 70 preselect threshold (single-
        #     keyword coincidences shouldn't auto-bind on their own);
        #     2+ terms can comfortably reach preselect territory.
        #   • Specificity adjustment — favors products whose description is
        #     densely about the shared terms over long generic descriptions.
        #   • Domain-match boost — products in the same domain as the
        #     consumer get +25. The wizard's marketplace fetch is already
        #     domain-scoped, so this almost always applies and pushes
        #     reasonable matches into preselect range. The marketplace
        #     filter alone is meaningful signal worth representing.
        base_score = min(80, 38 + overlap_count * 12)
        specificity = overlap_count / max(1, len(product_tokens))
        specificity_adj = min(18, int(specificity * 100))
        score = base_score + specificity_adj
        domain_match = (
            hint_domain
            and (p.get("domain") or "").strip().lower() == hint_domain
        )
        if domain_match:
            score += 25
        score = max(15, min(100, score))
        shared = sorted(overlap)[:6]
        if overlap:
            rationale = (
                f"Shares {overlap_count} term{'' if overlap_count == 1 else 's'} "
                f"with this consumer ({', '.join(shared)})"
                + (f" and is in the same domain (`{hint_domain}`)." if domain_match else ".")
            )
        else:
            rationale = (
                f"Same domain (`{hint_domain}`) as this consumer, though the "
                "descriptions don't share specific keywords yet."
            )
        scored.append(
            {
                "uri": p.get("uri"),
                "score": score,
                "rationale": rationale,
                # Surface the domain-match signal so the caller can lower the
                # preselect threshold for same-domain matches (where the wizard
                # has already filtered the pool to the consumer's domain, so a
                # match is high-confidence even with sparse text overlap).
                "domain_matched": bool(domain_match),
            }
        )
    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored[:5]


def _spec_discovery_hint(spec: dict) -> Optional[dict[str, Any]]:
    """Build a single "what does this consumer need?" hint from the spec when
    nothing was explicitly declared. Used to seed a discovery slot so the
    wizard's partial spec still produces ranked marketplace candidates even
    without an LLM-driven classifier.

    Returns ``None`` if the spec is too thin to extract any signal.
    """
    name = (spec.get("name") or "").strip()
    description = (spec.get("description") or "").strip()
    purpose = (spec.get("purpose") or "").strip()

    # Aggregate schema column hints. A consumer asking for {customer_id, order_total,
    # shipping_country} is plausibly pulling from a sales/orders source even with
    # no description prose at all.
    schema_columns: list[str] = []
    raw_schema = spec.get("schema")
    if isinstance(raw_schema, list):
        for tbl in raw_schema:
            if not isinstance(tbl, dict):
                continue
            props = tbl.get("properties")
            if not isinstance(props, list):
                continue
            for p in props:
                if not isinstance(p, dict):
                    continue
                col = (p.get("name") or p.get("physicalName") or "").strip()
                if col:
                    schema_columns.append(col)

    parts = [t for t in (description, purpose, " ".join(schema_columns)) if t]
    if not parts and not name:
        return None
    encompasses = " ".join(parts).strip()
    return {
        "name": name or "Sources that supply data for this consumer",
        "domain": (spec.get("domain") or "").strip().lower() or None,
        "encompasses": encompasses,
    }


# ── Models ────────────────────────────────────────────────────────────────


class ParseOdcsBody(BaseModel):
    content: str
    source: Optional[str] = None  # 'yaml' or 'json'; auto-detected if omitted


class ClassifyArchetypeBody(BaseModel):
    spec: dict


class InferredDependency(BaseModel):
    name: str = ""
    domain: Optional[str] = None
    encompasses: Optional[str] = None


class MatchInputsBody(BaseModel):
    spec: dict
    inferred_dependencies: list[dict] = []


class InputSelection(BaseModel):
    """One slot decision in the resolve-and-bind step.

    ``resolution`` values:
      * ``'matched'`` — bound to a marketplace product; ``dprod_uri`` / ``contract_id`` set.
      * ``'gap'`` — the PO acknowledged no good match; may carry ``spawned_request_id``
        if Create-now was used to launch a source wizard.

    The commit endpoint refuses ``dpe-cf`` ingest unless every slot is
    ``'matched'`` — the server-side gate that mirrors the UI submit-disable.
    """

    slot_id: str
    resolution: str  # 'matched' | 'gap'
    dprod_uri: Optional[str] = None
    contract_id: Optional[str] = None
    name: Optional[str] = None
    spawned_request_id: Optional[int] = None
    spawned_project_id: Optional[int] = None
    gap_suggestion: Optional[dict] = None


class FromOdcsBody(BaseModel):
    spec: dict
    owner_email: str
    notes: Optional[str] = None
    submit_to_engineer: bool = True
    archetype_override: Optional[str] = None  # 'dpe-sa' | 'dpe-cf'; falls back to 'dpe-sa'
    input_selections: list[InputSelection] = []
    ingest_draft_id: Optional[int] = None


class IngestDraftBody(BaseModel):
    """Upsert payload for /drafts. When ``id`` is set, the existing row is
    updated; otherwise a new row is created and the new ``id`` is returned.
    """

    id: Optional[int] = None
    owner_email: str
    source_filename: Optional[str] = None
    parsed_spec_json: str = "{}"
    classification_json: Optional[str] = None
    archetype_choice: Optional[str] = None
    input_selections_json: Optional[str] = None
    status: Optional[str] = None  # caller may flip to 'abandoned' explicitly


class IngestDraftPatchBody(BaseModel):
    """Partial update — typically used by NewSourceProductWizard to stamp a
    slot's ``spawned_request_id`` / ``spawned_project_id`` after spawning a
    source product."""

    classification_json: Optional[str] = None
    archetype_choice: Optional[str] = None
    input_selections_json: Optional[str] = None
    status: Optional[str] = None
    slot_update: Optional[dict] = None  # {slot_id, spawned_request_id, spawned_project_id}


# ── Helpers ───────────────────────────────────────────────────────────────


def _parse_raw(content: str, source: Optional[str]) -> dict:
    """Parse the raw content as ODCS YAML or JSON. Auto-detect by stripping
    whitespace and checking for a leading '{' or '['."""
    text = (content or "").strip()
    if not text:
        raise HTTPException(400, "Empty content")
    fmt = (source or "").lower().strip()
    if not fmt:
        fmt = "json" if text[:1] in ("{", "[") else "yaml"
    try:
        if fmt == "json":
            data = _json.loads(text)
        else:
            data = yaml.safe_load(text)
    except Exception as e:
        raise HTTPException(400, f"Failed to parse {fmt.upper()}: {e}")
    if not isinstance(data, dict):
        raise HTTPException(400, "ODCS spec must be a top-level object/mapping")
    return data


def _validate_canonical(spec: dict) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) on a canonicalised ODCS spec.

    Errors block submission; warnings are surfaced on the confirmation
    screen but don't prevent it. Required fields are intentionally narrow —
    the ingest path is forgiving so partial specs can still come in and
    get patched up via the Edit wizard later.
    """
    errors: list[str] = []
    warnings: list[str] = []
    if not (spec.get("name") or "").strip():
        errors.append("Spec is missing a top-level `name`.")
    schemas = spec.get("schema") or []
    if not isinstance(schemas, list) or not schemas:
        errors.append("Spec must have at least one entry in `schema`.")
    else:
        total_props = 0
        for i, sc in enumerate(schemas):
            if not isinstance(sc, dict):
                errors.append(f"`schema[{i}]` is not an object.")
                continue
            props = sc.get("properties") or []
            total_props += len(props) if isinstance(props, list) else 0
        if total_props == 0:
            errors.append("Spec has no columns/properties on any schema.")
    if not (spec.get("domain") or "").strip():
        warnings.append("`domain` is empty — engineer-side starter rules and catalogs won't apply.")
    if not (spec.get("description") or "").strip():
        warnings.append("`description` is empty.")
    if not (spec.get("owners") or []):
        warnings.append("`owners` is empty — the imported product will not show up under any user's My Products.")
    return errors, warnings


def _ensure_owner(spec: dict, owner_email: str) -> dict:
    """Always make sure the submitting user appears in ``owners[]`` so the
    imported product surfaces in their My Products view (the marketplace
    owner filter matches by ``owners[].email``).

    - Empty owners list → the submitter becomes the sole owner.
    - Existing owners that already include the submitter → no change.
    - Existing owners *without* the submitter → submitter appended; the
      original list is preserved so other stewards/owners stay attached.
    """
    target = (owner_email or "").strip().lower()
    if not target:
        return spec
    owners = spec.get("owners") if isinstance(spec.get("owners"), list) else []
    already = any(
        isinstance(o, dict) and (o.get("email") or "").strip().lower() == target
        for o in owners
    )
    if already:
        return spec
    new_owner = {
        "username": owner_email,
        "name": owner_email,
        "role": "Data Product Owner",
        "email": owner_email,
    }
    spec = dict(spec)
    spec["owners"] = list(owners) + [new_owner]
    return spec


# ── Endpoints ─────────────────────────────────────────────────────────────


@router.post("/parse-odcs")
def parse_odcs(body: ParseOdcsBody):
    """Deterministic YAML/JSON parse + ODCS v3.1 canonicalisation. No
    side effects — used to populate the Confirmation step in the
    Ingest UI before commit."""
    raw = _parse_raw(body.content, body.source)
    canonical = _canonicalize_v3_1(raw)
    errors, warnings = _validate_canonical(canonical)
    return {
        "parsed": canonical,
        "errors": errors,
        "warnings": warnings,
    }


@router.post("/classify-archetype")
def classify_archetype(body: ClassifyArchetypeBody):
    """Semantically classify an ODCS spec as source-aligned or consumer-aligned.

    Invokes the ``data-product-archetype-classifier`` skill via the Claude
    Code SDK. On any SDK failure (skill not installed, parse error, etc.) the
    endpoint falls back to a cheap heuristic so the UI always has something
    to render — the response is tagged ``_fallback: true`` in that case.
    """
    spec = body.spec or {}
    if not isinstance(spec, dict) or not spec:
        raise HTTPException(400, "spec must be a non-empty object")

    try:
        payload, error = asyncio.run(
            _run_classifier_skill("classify_archetype", {"spec": spec})
        )
    except RuntimeError:
        # Already inside an event loop (uncommon for sync FastAPI handlers,
        # but defensively fall back rather than crashing the request).
        payload, error = {}, "Event loop already running"

    if error or not payload.get("kind"):
        payload = _heuristic_classify(spec)
        if error:
            payload["_error"] = error

    kind = (payload.get("kind") or "").strip().lower()
    if kind not in ("source", "consumer"):
        payload["kind"] = "source"
    payload.setdefault("confidence", 0)
    payload.setdefault("rationale", "")
    payload.setdefault("signals", [])
    payload.setdefault("inferred_dependencies", [])
    return payload


@router.post("/match-inputs")
def match_inputs(body: MatchInputsBody, session: Session = Depends(get_session)):
    """Rank published upstream products against the spec's declared inputs
    and the classifier's inferred dependencies. Returns per-slot candidates
    with score + rationale, a preselected URI, and a gap suggestion for slots
    with no acceptable match.

    The candidate pool is EVERY published product — source-aligned, aggregate,
    or consumer-aligned — since a consumer may build on any of them (multi-hop
    chains are supported). The DAG guard at save time refuses a self-loop or
    cycle, so the picker only pre-excludes the consumer's own contract.

    Matching tiers:
      1. Exact ``dprod_uri`` match against the marketplace (score 100).
      2. Exact normalised-name match within the spec's domain (score 90).
      3. Semantic match via the classifier skill, ``MODE: match_inputs``
         (skill returns ranked URIs with rationale).

    Slots are seeded from ``spec.inputs[]`` first, then from
    ``inferred_dependencies`` for any dependencies the spec didn't already
    declare. Slot ids are stable across calls for the same spec so the UI
    can keep selections coherent.
    """
    spec = body.spec or {}
    spec_domain = (spec.get("domain") or "").strip().lower() or None

    # The candidate pool is every PUBLISHED product (source / aggregate /
    # consumer). Reuse the in-process listing rather than an HTTP call — same
    # code path the UI uses. list_published falls back to a DRAFT cv when a
    # product has no deployed version, so filter explicitly: keep only rows that
    # are actually consumable (published/superseded lifecycle) with an allowed
    # productKind (excludes empty-kind rows), and never offer the consumer its
    # own contract (a self-loop the DAG guard would reject).
    self_contract_id = (spec.get("id") or "").strip()
    self_uri = f"dprod:{self_contract_id}" if self_contract_id else ""
    _consumable = {s.lower() for s in CONSUMABLE_LIFECYCLE_STATES}
    _allowed_kinds = {k.lower() for k in ALLOWED_UPSTREAM_PRODUCT_KINDS}
    try:
        # Pass product_kind=None EXPLICITLY: list_published is a FastAPI route
        # whose product_kind defaults to a Query object (not None) when called
        # in-process, which its (product_kind or "").strip() would choke on. We
        # want all kinds here anyway; the _allowed_kinds/lifecycle filter below
        # narrows the pool.
        marketplace_resp = _marketplace.list_published(
            session=session, owned_by=None, product_kind=None,
        )
        raw_pool = marketplace_resp.get("products") or []
    except Exception as e:
        # Don't silently mask a pool-resolution failure as "everything needs
        # creating" — surface it in the logs (the empty pool still degrades to
        # gap suggestions, but now it's diagnosable).
        logger.warning("match_inputs: candidate pool resolution failed: %s", e)
        raw_pool = []
    all_sources = [
        p for p in raw_pool
        if (p.get("lifecycle_state") or "").strip().lower() in _consumable
        and (p.get("product_kind") or "").strip().lower() in _allowed_kinds
        and (p.get("uri") or "") != self_uri
        and (p.get("contract_id") or "") != self_contract_id
    ]

    # Optionally restrict to spec.domain for the semantic + name match tiers.
    # Exact-URI matches are domain-agnostic — a typo in the spec's domain
    # shouldn't drop a perfectly valid URI match.
    domain_scoped = (
        [p for p in all_sources if (p.get("domain") or "").lower() == spec_domain]
        if spec_domain
        else all_sources
    )
    compact_pool = [_compact_marketplace_row(p) for p in domain_scoped]

    by_uri = {p.get("uri"): p for p in all_sources if p.get("uri")}
    by_norm_name = {_normalised_name(p.get("name")): p for p in domain_scoped if p.get("name")}

    # ── Seed slots from spec.inputs[] + inferred_dependencies ──────────
    raw_inputs = spec.get("inputs") or []
    declared_slots: list[dict[str, Any]] = []
    declared_names: set[str] = set()
    if isinstance(raw_inputs, list):
        for idx, entry in enumerate(raw_inputs):
            if not isinstance(entry, dict):
                continue
            decl_uri = (entry.get("dprod_uri") or entry.get("dprodUri") or "").strip()
            decl_name = (entry.get("name") or "").strip()
            if not decl_uri and not decl_name:
                continue
            declared_slots.append(
                {
                    "slot_id": f"input-{idx}",
                    "source": "spec_inputs",
                    "declared": {"dprod_uri": decl_uri or None, "name": decl_name or None},
                    "encompasses_hint": None,
                }
            )
            if decl_name:
                declared_names.add(_normalised_name(decl_name))

    inferred_slots: list[dict[str, Any]] = []
    for idx, dep in enumerate(body.inferred_dependencies or []):
        if not isinstance(dep, dict):
            continue
        dep_name = (dep.get("name") or "").strip()
        if not dep_name:
            continue
        # Skip dependencies that are already covered by an explicit spec.inputs entry.
        if _normalised_name(dep_name) in declared_names:
            continue
        inferred_slots.append(
            {
                "slot_id": f"inferred-{idx}",
                "source": "inferred",
                "declared": {"dprod_uri": None, "name": dep_name},
                "encompasses_hint": (dep.get("encompasses") or "").strip() or None,
            }
        )

    slots = declared_slots + inferred_slots

    # Discovery slot: when nothing was explicitly declared or inferred but the
    # spec carries text/schema signal, synthesise one open-ended "what supplies
    # this consumer?" slot so the wizard's partial spec still produces ranked
    # marketplace candidates. The classifier skill would normally emit the
    # `inferred_dependencies` that drive this; the discovery slot keeps the
    # path useful when that skill isn't installed.
    if not slots:
        discovery_hint = _spec_discovery_hint(spec)
        if discovery_hint:
            slots = [
                {
                    "slot_id": "discovery-0",
                    "source": "inferred",
                    "declared": {"dprod_uri": None, "name": discovery_hint["name"]},
                    "encompasses_hint": discovery_hint.get("encompasses"),
                }
            ]

    # ── Resolve each slot through the three-tier matching cascade ──────
    result_slots: list[dict[str, Any]] = []
    for slot in slots:
        decl = slot["declared"] or {}
        decl_uri = decl.get("dprod_uri")
        decl_name = decl.get("name")

        candidates: list[dict[str, Any]] = []
        preselected_uri: Optional[str] = None

        # Tier 1: exact URI
        if decl_uri and decl_uri in by_uri:
            row = by_uri[decl_uri]
            candidates.append(
                {
                    **_compact_marketplace_row(row),
                    "match_score": 100,
                    "match_kind": "exact_uri",
                    "rationale": "Exact dprod_uri match against the marketplace.",
                }
            )
            preselected_uri = decl_uri

        # Tier 2: exact normalised name within the spec's domain
        if not preselected_uri and decl_name:
            norm = _normalised_name(decl_name)
            if norm and norm in by_norm_name:
                row = by_norm_name[norm]
                candidates.append(
                    {
                        **_compact_marketplace_row(row),
                        "match_score": 90,
                        "match_kind": "exact_name",
                        "rationale": f"Exact name match (normalised) within domain '{spec_domain or '*'}'.",
                    }
                )
                preselected_uri = row.get("uri")

        # Tier 3: semantic match via the classifier skill, with a deterministic
        # keyword-overlap fallback when the skill isn't installed or returns
        # nothing useful. The fallback ensures the wizard's partial-spec call
        # always produces ranked candidates instead of an empty slot list.
        if not preselected_uri and compact_pool:
            slot_hint = {
                "name": decl_name,
                "encompasses": slot.get("encompasses_hint"),
                "domain": spec_domain,
            }
            try:
                payload, _err = asyncio.run(
                    _run_classifier_skill(
                        "match_inputs",
                        {"slot_hint": slot_hint, "marketplace_products": compact_pool},
                    )
                )
            except RuntimeError:
                payload = {}
            ranked = payload.get("ranked") or []
            used_heuristic = False
            if not ranked:
                ranked = _heuristic_rank_marketplace(slot_hint, compact_pool)
                used_heuristic = True
            for rank_entry in ranked[:5]:
                if not isinstance(rank_entry, dict):
                    continue
                uri = rank_entry.get("uri")
                if not uri or uri not in by_uri:
                    continue
                score = int(rank_entry.get("score") or 0)
                rationale = (rank_entry.get("rationale") or "").strip()
                if used_heuristic and not rationale:
                    rationale = "Matched on description / name keyword overlap."
                candidates.append(
                    {
                        **_compact_marketplace_row(by_uri[uri]),
                        "match_score": score,
                        "match_kind": "semantic",
                        "rationale": rationale,
                    }
                )
                # Domain-matched candidates preselect at a lower threshold
                # (65 vs 75) because the wizard already filtered the pool to
                # the consumer's domain — a match in-domain is a strong
                # signal, but it has to clear a "meaningful semantic overlap"
                # bar, not just "happens to share a domain." The skill's own
                # scoring guidance asks for ≥65 only when there's real
                # name + encompasses overlap (see classifier SKILL.md
                # Ranking rules); previously the backend threshold was 55,
                # which let in-domain-but-semantically-weak candidates get
                # promoted to "Recommended" in the UI.
                threshold = 65 if rank_entry.get("domain_matched") else 75
                if score >= threshold and not preselected_uri:
                    preselected_uri = uri

        # Gap suggestion: invoke synthesize_missing_source if no preselect.
        gap_suggestion: Optional[dict[str, Any]] = None
        if not preselected_uri:
            slot_hint = {
                "name": decl_name,
                "encompasses": slot.get("encompasses_hint"),
                "domain": spec_domain,
            }
            try:
                payload, _err = asyncio.run(
                    _run_classifier_skill("synthesize_missing_source", {"slot_hint": slot_hint})
                )
                if payload.get("name"):
                    gap_suggestion = {
                        "name": payload.get("name"),
                        "domain": payload.get("domain") or spec_domain,
                        "encompasses": payload.get("encompasses") or slot.get("encompasses_hint") or "",
                        "minimal_columns": payload.get("minimal_columns") or [],
                    }
            except RuntimeError:
                pass
            if not gap_suggestion:
                # Cheap fallback so the UI always has *something* to prefill.
                gap_suggestion = {
                    "name": decl_name or "New source product",
                    "domain": spec_domain,
                    "encompasses": slot.get("encompasses_hint") or "",
                    "minimal_columns": [],
                }

        # Confidence band — explicit signal to the UI about how strongly
        # the preselection should be presented. `strong` lets the chip
        # show "✓ Recommended"; `tentative` lets it temper to "⚠ Tentative
        # match" with a more visible "request new source" CTA; `gap`
        # means no preselect and the slot renders as "● Needs new source"
        # (with candidates[] available as "Considered candidates (low
        # confidence)" disclosure for engineer override).
        top_score = candidates[0].get("match_score") if candidates else 0
        if not preselected_uri:
            confidence_band = "gap"
        elif (top_score or 0) >= 80:
            confidence_band = "strong"
        else:
            confidence_band = "tentative"

        result_slots.append(
            {
                "slot_id": slot["slot_id"],
                "source": slot["source"],
                "declared": decl,
                "encompasses_hint": slot.get("encompasses_hint"),
                "candidates": candidates,
                "preselected_candidate_uri": preselected_uri,
                "confidence_band": confidence_band,
                "gap_suggestion": gap_suggestion,
            }
        )

    return {"slots": result_slots}


# ── Drafts CRUD ──────────────────────────────────────────────────────────


def _serialise_draft(d: IngestDraft) -> dict[str, Any]:
    return {
        "id": d.id,
        "owner_email": d.owner_email,
        "created_at": d.created_at.isoformat() if d.created_at else None,
        "updated_at": d.updated_at.isoformat() if d.updated_at else None,
        "source_filename": d.source_filename,
        "parsed_spec_json": d.parsed_spec_json,
        "classification_json": d.classification_json,
        "archetype_choice": d.archetype_choice,
        "input_selections_json": d.input_selections_json,
        "status": d.status,
        "committed_project_id": d.committed_project_id,
    }


@router.post("/drafts")
def upsert_draft(body: IngestDraftBody, session: Session = Depends(get_session)):
    """Create or update an in-flight ingest draft. Pass ``id`` to update an
    existing row; omit it to create a new draft. Returns the persisted row.
    """
    if body.id is not None:
        draft = session.get(IngestDraft, body.id)
        if not draft:
            raise HTTPException(404, f"IngestDraft {body.id} not found")
        if (draft.owner_email or "").lower() != (body.owner_email or "").lower():
            raise HTTPException(403, "Draft is owned by a different user")
    else:
        draft = IngestDraft(owner_email=body.owner_email)

    draft.source_filename = body.source_filename
    draft.parsed_spec_json = body.parsed_spec_json or "{}"
    if body.classification_json is not None:
        draft.classification_json = body.classification_json
    if body.archetype_choice is not None:
        draft.archetype_choice = body.archetype_choice
    if body.input_selections_json is not None:
        draft.input_selections_json = body.input_selections_json
    if body.status:
        draft.status = body.status
    draft.updated_at = datetime.utcnow()
    session.add(draft)
    session.commit()
    session.refresh(draft)
    return _serialise_draft(draft)


@router.get("/drafts")
def list_drafts(
    owner_email: str,
    status: Optional[str] = None,
    session: Session = Depends(get_session),
):
    """List drafts owned by the given email. Filter by status if provided.
    Newest-updated first."""
    statement = select(IngestDraft).where(IngestDraft.owner_email == owner_email)
    if status:
        statement = statement.where(IngestDraft.status == status)
    rows = session.exec(statement).all()
    rows = sorted(rows, key=lambda r: r.updated_at or r.created_at, reverse=True)
    return {"drafts": [_serialise_draft(d) for d in rows], "count": len(rows)}


@router.get("/drafts/{draft_id}")
def get_draft(draft_id: int, session: Session = Depends(get_session)):
    draft = session.get(IngestDraft, draft_id)
    if not draft:
        raise HTTPException(404, f"IngestDraft {draft_id} not found")
    return _serialise_draft(draft)


@router.patch("/drafts/{draft_id}")
def patch_draft(
    draft_id: int,
    body: IngestDraftPatchBody,
    session: Session = Depends(get_session),
):
    """Partial update. ``slot_update`` is a convenience field used by
    ``NewSourceProductWizard`` to stamp a slot's ``spawned_request_id`` and
    ``spawned_project_id`` without round-tripping the entire selections list
    through the client."""
    draft = session.get(IngestDraft, draft_id)
    if not draft:
        raise HTTPException(404, f"IngestDraft {draft_id} not found")

    if body.classification_json is not None:
        draft.classification_json = body.classification_json
    if body.archetype_choice is not None:
        draft.archetype_choice = body.archetype_choice
    if body.input_selections_json is not None:
        draft.input_selections_json = body.input_selections_json
    if body.status:
        draft.status = body.status

    if body.slot_update:
        target_slot_id = (body.slot_update.get("slot_id") or "").strip()
        if target_slot_id:
            current = []
            try:
                current = _json.loads(draft.input_selections_json or "[]")
            except _json.JSONDecodeError:
                current = []
            if not isinstance(current, list):
                current = []
            patched = False
            for entry in current:
                if isinstance(entry, dict) and (entry.get("slot_id") == target_slot_id):
                    for k in ("spawned_request_id", "spawned_project_id"):
                        if k in body.slot_update:
                            entry[k] = body.slot_update[k]
                    patched = True
                    break
            if not patched:
                # Slot didn't exist yet (e.g. created from an inferred dep that
                # never got a UI row). Append it so cross-link is preserved.
                new_entry = {
                    "slot_id": target_slot_id,
                    "resolution": "gap",
                    **{
                        k: body.slot_update[k]
                        for k in ("spawned_request_id", "spawned_project_id")
                        if k in body.slot_update
                    },
                }
                current.append(new_entry)
            draft.input_selections_json = _json.dumps(current)

    draft.updated_at = datetime.utcnow()
    session.add(draft)
    session.commit()
    session.refresh(draft)
    return _serialise_draft(draft)


@router.post("/from-odcs")
def ingest_from_odcs(body: FromOdcsBody, session: Session = Depends(get_session)):
    """Create a project scaffolded for ingest, persist the contract + DPROD
    subgraph, and enqueue a ProductRequest of ``kind='ingest'``.

    Branches by ``archetype_override``:

    * ``dpe-sa`` (default) — keeps the existing ingest-only workflow set,
      auto-publishes the DPROD subgraph (spec is authoritative).
    * ``dpe-cf`` — scaffolds the consumer default workflows
      (product_definition → odcs_to_dprod → integration), MERGEs ``:CONSUMES``
      edges from ``input_selections`` (matched only), refuses commit if any
      slot is unresolved, lands at ``lifecycleState='submitted'`` without
      auto-publishing — engineer needs to run data_mapping + serving.

    On success, marks the linked ``IngestDraft`` (if any) as committed.
    """
    archetype = (body.archetype_override or "dpe-sa").strip().lower()
    if archetype not in ("dpe-sa", "dpe-cf"):
        raise HTTPException(400, f"Unsupported archetype_override: {archetype}")

    spec = _ensure_owner(body.spec or {}, body.owner_email.strip())
    errors, _ = _validate_canonical(spec)
    if errors:
        raise HTTPException(400, {"message": "Spec failed validation", "errors": errors})

    # Server-side submit gate for cf: every slot must be 'matched' or the
    # commit is refused. UI prevents this, but the gate is the source of
    # truth — engineer's view should never receive a half-bound contract.
    if archetype == "dpe-cf" and body.submit_to_engineer:
        unresolved = [
            s.slot_id
            for s in (body.input_selections or [])
            if s.resolution != "matched" or not (s.dprod_uri or "").strip()
        ]
        if unresolved:
            raise HTTPException(
                400,
                {
                    "message": "Consumer-aligned ingest cannot submit until every source input is bound to a published source product.",
                    "unresolved_slots": unresolved,
                },
            )

    # For cf: build spec.inputs[] from the matched selections so that
    # _save_odcs_to_graph → sync_consumes_edges MERGEs the right :CONSUMES.
    if archetype == "dpe-cf":
        matched = [s for s in (body.input_selections or []) if s.resolution == "matched"]
        spec["inputs"] = [
            {
                "dprod_uri": s.dprod_uri,
                "contract_id": s.contract_id,
                "name": s.name or "",
            }
            for s in matched
            if (s.dprod_uri or "").strip()
        ]
    else:
        # For sa: drop any inputs[] that crept in from the spec — source
        # products don't consume anything in the workbench model.
        if "inputs" in spec:
            spec["inputs"] = []

    project_name = (spec.get("name") or "Imported Product").strip()
    domain = (spec.get("domain") or "").strip().lower() or None

    # Workflow scaffolding diverges by archetype. The cf path uses the
    # default consumer template (lean: product_definition + odcs_to_dprod +
    # integration). The sa path keeps the historic ingest-only triple, which
    # bypasses the discovery/profiling/enrichment pipeline because the
    # imported spec is authoritative.
    if archetype == "dpe-cf":
        # Empty workflow_ids → create_project applies DEFAULT_WORKFLOW_TEMPLATES
        # for the archetype. That's exactly what we want for cf.
        workflow_ids: list[str] = []
    else:
        workflow_ids = ["odcs_to_dprod", "lineage_discovery", "marketplace"]

    project_create = ProjectCreate(
        name=project_name,
        archetype=archetype,
        domain=domain,
        workflow_ids=workflow_ids,
    )
    try:
        project_response = create_project(project_create, session)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Project scaffolding failed: {e}")

    project_id = (
        project_response["id"] if isinstance(project_response, dict) else getattr(project_response, "id", None)
    )
    if project_id is None:
        raise HTTPException(500, "Project creation returned no id")
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(500, "Project row vanished after creation")

    contract_id = f"{project.project_code}-contract"
    spec["id"] = contract_id

    try:
        _save_odcs_to_graph(spec, project, submitted_by=body.owner_email)
    except ConsumesBindingError as e:
        # A refused upstream binding (self / cycle / ineligible target). The
        # project row was created above; surface a clean 409 so the caller can
        # fix the inputs and retry rather than seeing an opaque 500.
        raise HTTPException(
            409,
            {"message": str(e), "reason": e.reason, "offending_uri": e.offending_uri},
        )
    except Exception as e:
        raise HTTPException(500, f"ODCS persistence failed: {e}")

    # The consumer's :CONSUMES edges just synced — flip any matching intake
    # pending-dependencies to 'bound'. Best-effort; a dpe-sa ingest is a no-op.
    try:
        from ..intake_scaffold import resolve_pending_dependencies
        resolve_pending_dependencies(session, project, spec.get("inputs"))
    except Exception:
        pass

    try:
        _generate_dprod(contract_id, project)
    except Exception:
        # Best-effort: engineer can re-run odcs_to_dprod from the pipeline.
        pass

    versioned_id: Optional[str] = None
    if archetype == "dpe-sa":
        # SA: spec is authoritative; auto-publish without an engineer pipeline.
        try:
            with neo4j_session(
                project.neo4j_host, project.neo4j_port,
                project.neo4j_user, project.neo4j_password, project.neo4j_database,
            ) as ns:
                row = ns.run(
                    "MATCH (dc:DataContract {id: $cid}) "
                    "SET dc.currentLifecycleState = 'published' "
                    "RETURN (dc.id + ':v' + toString(dc.currentVersion)) AS versioned_id",
                    cid=contract_id,
                ).single()
                if row:
                    versioned_id = row["versioned_id"]
                ns.run(
                    "MATCH (dp:DProdDataProduct) "
                    "WHERE dp.uri STARTS WITH 'dprod:' + $project_code "
                    "SET dp.status = 'published', "
                    "    dp.publishedAt = datetime(), "
                    "    dp.publishedBy = $user",
                    project_code=project.project_code,
                    user=body.owner_email,
                )
        except Exception:
            pass

        try:
            project.published_at = datetime.utcnow()
            session.add(project)
        except Exception:
            pass
    else:
        # CF: land at 'submitted' so the engineer's Incoming queue can pick
        # it up and run data_mapping / serving. Capture the versioned_id for
        # the ProductRequest row so the engineer's banner can deep-link.
        try:
            with neo4j_session(
                project.neo4j_host, project.neo4j_port,
                project.neo4j_user, project.neo4j_password, project.neo4j_database,
            ) as ns:
                row = ns.run(
                    "MATCH (dc:DataContract {id: $cid}) "
                    "RETURN (dc.id + ':v' + toString(dc.currentVersion)) AS versioned_id",
                    cid=contract_id,
                ).single()
                if row:
                    versioned_id = row["versioned_id"]
        except Exception:
            pass

    product_request_id: Optional[int] = None
    if body.submit_to_engineer:
        request = ProductRequest(
            project_id=project_id,
            contract_id=contract_id,
            contract_versioned_id=versioned_id,
            kind=ProductRequestKind.ingest,
            status=ProductRequestStatus.submitted,
            submitted_by=body.owner_email,
            notes=(body.notes or "Imported from ODCS spec").strip(),
            parent_ingest_draft_id=body.ingest_draft_id,
        )
        session.add(request)
        session.commit()
        session.refresh(request)
        product_request_id = request.id
    else:
        session.commit()

    # Mark the linked IngestDraft as committed so it falls off the in-flight
    # list and points the resume page at the new project.
    if body.ingest_draft_id is not None:
        draft = session.get(IngestDraft, body.ingest_draft_id)
        if draft is not None:
            draft.status = "committed"
            draft.committed_project_id = project_id
            draft.updated_at = datetime.utcnow()
            session.add(draft)
            session.commit()

    return {
        "project_id": project_id,
        "project_code": project.project_code,
        "contract_id": contract_id,
        "versioned_id": versioned_id,
        "product_request_id": product_request_id,
        "archetype": archetype,
        "status": (
            "submitted" if body.submit_to_engineer
            else ("published" if archetype == "dpe-sa" else "draft")
        ),
    }


# ── Pre-flight gap analysis (step 8 of the consumer wizard) ──────────────


GAP_ANALYZER_SKILL = "data-product-gap-analyzer"


class GapAnalysisColumnInput(BaseModel):
    name: str
    logical_type: Optional[str] = None
    description: Optional[str] = None


class GapAnalysisBody(BaseModel):
    consumer_idea: Optional[str] = None
    consumer_description: Optional[str] = None
    consumer_domain: Optional[str] = None
    consumer_columns: list[GapAnalysisColumnInput] = []
    candidate_contract_ids: list[str] = []


async def _run_gap_analyzer_skill(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], Optional[str]]:
    """Invoke the data-product-gap-analyzer skill via the SDK. Mirrors
    ``_run_classifier_skill`` (single-mode, fenced-JSON output, returns
    ``(payload, error)``)."""
    try:
        from claude_agent_sdk import query, ClaudeAgentOptions
        from claude_agent_sdk.types import (
            AssistantMessage,
            ResultMessage,
            TextBlock,
        )
    except ImportError:
        return {}, "claude-agent-sdk is not installed"

    system_prompt = (
        f"FIRST: Load the `{GAP_ANALYZER_SKILL}` skill via the Skill tool, then "
        "follow its instructions to the letter. Emit exactly one fenced JSON "
        "code block as the skill instructs. Do not write files. Do not run "
        "shell commands. Do not answer in prose outside the JSON block."
    )

    user_prompt = (
        f"FIRST: Load the {GAP_ANALYZER_SKILL} skill using the Skill tool.\n\n"
        f"INPUTS (JSON):\n{_json.dumps(payload, ensure_ascii=False)}\n\n"
        "Output the single fenced JSON block as instructed by the skill."
    )

    options = ClaudeAgentOptions(
        allowed_tools=["Read", "Skill"],
        permission_mode="acceptEdits",
        cwd=str(BASE_DIR),
        max_turns=4,
        skills="all",
        plugins=PIPELINE_PLUGINS,
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": system_prompt,
        },
    )

    transcript_parts: list[str] = []
    try:
        async for message in query(prompt=user_prompt, options=options):
            if message is None:
                continue
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        transcript_parts.append(block.text)
            elif isinstance(message, ResultMessage):
                from ..llm_usage import extract_usage, record_usage
                record_usage(source="gap_analyzer", usage=extract_usage(message))
                if message.is_error:
                    return {}, "Gap analyzer returned an error"
    except Exception as e:
        return {}, f"Gap analyzer failed: {e}"

    out = _parse_skill_json("\n".join(transcript_parts))
    if not isinstance(out, dict):
        return {}, "Gap analyzer output not in the expected shape"
    return out, None


def _heuristic_gap_analyze(
    consumer_columns: list[dict[str, Any]],
    candidate_sources: list[dict[str, Any]],
) -> dict[str, Any]:
    """Deterministic keyword-overlap gap analyzer. Used as a fallback when
    the LLM skill is unavailable. Per-column status:

      • ``covered``    — exact-name match on at least one source column.
      • ``derivable``  — at least one source-column shares ≥2 normalised
                         tokens with the consumer column (likely composable).
      • ``ambiguous``  — exactly one shared token (weak signal).
      • ``gap``        — no token overlap with any source column.

    Not comprehensive — intended as a "surfaces obvious cases" baseline so the
    endpoint always returns something useful even without the skill installed.
    """
    # Flatten the source columns into a pool with their origin metadata so we
    # can construct readable source_evidence strings.
    flat_sources: list[dict[str, Any]] = []
    for src in candidate_sources:
        product = src.get("product_name") or src.get("contract_id") or "source"
        for col in src.get("columns") or []:
            cname = (col.get("name") or "").strip()
            if not cname:
                continue
            # Table-name context matters at column granularity — `product.name`
            # is semantically a product-name, distinct from `customer.name`.
            # Include the source dataset name AND the table description in
            # the token bag so consumer columns like `product_name` find
            # their match by the table-noun overlap, not just the column
            # name (which is often a generic like "name" / "id" / "code").
            table = (col.get("dataset") or "").strip()
            table_desc = (col.get("table_description") or "").strip()
            composite_norm = _normalised_name(f"{table}_{cname}") if table else ""
            flat_sources.append(
                {
                    "product": product,
                    "name": cname,
                    "table": table,
                    "display": f"{table}.{cname}" if table else cname,
                    "norm": _normalised_name(cname),
                    "composite_norm": composite_norm,
                    "tokens": _tokenise_for_similarity(
                        f"{table} {cname} {col.get('description') or ''} {table_desc}",
                        stopwords=_COLUMN_LEVEL_STOPWORDS,
                    ),
                }
            )

    # Generic suffix tokens that show up in column names everywhere and
    # carry no real semantic signal on their own. A match on these alone
    # (e.g. customer_id vs nasa_sat_id sharing only `id`) is a false positive.
    _COLUMN_NAME_GENERICS = frozenset({
        "id", "ids", "key", "keys", "code", "codes", "name", "names",
        "value", "values", "type", "types", "kind", "kinds",
        "count", "counts", "num", "nums", "amount", "amounts",
        "date", "dates", "time", "times", "ts", "ref", "refs",
        "status", "uri", "url",
    })

    def _meaningful(tokens: set[str]) -> set[str]:
        return tokens - _COLUMN_NAME_GENERICS

    gaps: list[dict[str, Any]] = []
    counts = {"covered": 0, "derivable": 0, "ambiguous": 0, "gap": 0}
    for c in consumer_columns:
        cname = (c.get("name") or "").strip()
        if not cname:
            continue
        c_norm = _normalised_name(cname)
        c_tokens = _tokenise_for_similarity(
            f"{cname} {c.get('description') or ''}",
            stopwords=_COLUMN_LEVEL_STOPWORDS,
        )
        # Tier 1: exact normalised-name match on the column name alone.
        exact = [s for s in flat_sources if s["norm"] == c_norm]
        if exact:
            ev = [f"{s['product']}.{s['display']}" for s in exact[:3]]
            counts["covered"] += 1
            gaps.append(
                {
                    "column_name": cname,
                    "status": "covered",
                    "confidence": 90,
                    "source_evidence": ev,
                    "rationale": f"Direct name match on {len(exact)} source column(s).",
                }
            )
            continue
        # Tier 1.5: composite match — consumer `product_name` ↔ source
        # `product.name` (table+column concatenated and normalised). Catches
        # the very common case where a consumer column carries the
        # table-noun prefix that the source schema split out into the
        # table name.
        composite = [s for s in flat_sources if s["composite_norm"] and s["composite_norm"] == c_norm]
        if composite:
            ev = [f"{s['product']}.{s['display']}" for s in composite[:3]]
            counts["covered"] += 1
            gaps.append(
                {
                    "column_name": cname,
                    "status": "covered",
                    "confidence": 85,
                    "source_evidence": ev,
                    "rationale": f"Composite name match: consumer '{cname}' ↔ source {ev[0]} (table prefix + column).",
                }
            )
            continue
        # Tier 2/3: token-overlap ranking, weighting by *meaningful* tokens
        # (overlap on generic suffixes like `id` / `code` doesn't count).
        ranked: list[tuple[int, int, dict[str, Any]]] = []
        for s in flat_sources:
            overlap = c_tokens & s["tokens"]
            meaningful = _meaningful(overlap)
            if meaningful:
                ranked.append((len(meaningful), len(overlap), s))
        ranked.sort(key=lambda t: (t[0], t[1]), reverse=True)
        if not ranked:
            counts["gap"] += 1
            gaps.append(
                {
                    "column_name": cname,
                    "status": "gap",
                    "confidence": 80,
                    "source_evidence": [],
                    "rationale": "No source column shares meaningful keywords with this consumer column.",
                }
            )
        else:
            best_meaningful, _, _ = ranked[0]
            ev = [f"{s['product']}.{s['display']}" for _, _, s in ranked[:3]]
            if best_meaningful >= 2:
                counts["derivable"] += 1
                gaps.append(
                    {
                        "column_name": cname,
                        "status": "derivable",
                        "confidence": 65,
                        "source_evidence": ev,
                        "rationale": "Probably composable from source column(s) sharing ≥2 meaningful keywords.",
                    }
                )
            else:
                counts["ambiguous"] += 1
                gaps.append(
                    {
                        "column_name": cname,
                        "status": "ambiguous",
                        "confidence": 40,
                        "source_evidence": ev,
                        "rationale": "Single weak keyword overlap — engineer will need to verify.",
                    }
                )
    summary = (
        f"{counts['covered']} covered, {counts['derivable']} derivable, "
        f"{counts['ambiguous']} ambiguous, {counts['gap']} gaps"
    )
    return {"gaps": gaps, "summary": summary, "_fallback": True}


@router.post("/projects/{project_id}/gap-analysis")
def gap_analysis_for_project(
    project_id: int,
    body: GapAnalysisBody,
    session: Session = Depends(get_session),
):
    """Run a baseline pre-flight gap analysis for a consumer-aligned project.

    Compares the consumer's authored schema columns against the per-column
    metadata of the picked candidate source-aligned data products. Returns
    per-column status (covered / derivable / ambiguous / gap) so the PO can
    address obvious mapping gaps before submitting to engineering.

    Invokes the ``data-product-gap-analyzer`` skill when available; falls
    back to a deterministic keyword-overlap heuristic so the endpoint always
    returns something useful (tagged ``_fallback: true``).
    """
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(404, f"Project {project_id} not found")

    # Materialize candidate-source column metadata via the existing helper.
    candidate_sources = _domain_catalogs._fetch_source_columns(
        session, body.candidate_contract_ids
    )

    consumer_columns = [
        {
            "name": c.name,
            "logical_type": c.logical_type or "",
            "description": c.description or "",
        }
        for c in body.consumer_columns
        if c.name
    ]

    if not consumer_columns:
        return {
            "gaps": [],
            "summary": "No consumer columns to analyse yet.",
            "_fallback": True,
        }
    if not candidate_sources:
        # Treat every column as a gap when no sources are bound.
        gaps = [
            {
                "column_name": c["name"],
                "status": "gap",
                "confidence": 95,
                "source_evidence": [],
                "rationale": "No candidate sources picked — every column is a gap by definition.",
            }
            for c in consumer_columns
        ]
        return {
            "gaps": gaps,
            "summary": f"0 covered, 0 derivable, 0 ambiguous, {len(gaps)} gaps",
            "_fallback": True,
            "_no_sources": True,
        }

    payload = {
        "consumer_idea": body.consumer_idea or "",
        "consumer_description": body.consumer_description or "",
        "consumer_domain": body.consumer_domain or "",
        "consumer_columns": consumer_columns,
        "candidate_sources": candidate_sources,
    }

    # Invoke the skill; fall back to heuristic if anything fails.
    try:
        skill_out, error = asyncio.run(_run_gap_analyzer_skill(payload))
    except RuntimeError:
        skill_out, error = {}, "Event loop already running"

    if error or not isinstance(skill_out.get("gaps"), list):
        heuristic = _heuristic_gap_analyze(consumer_columns, candidate_sources)
        if error:
            heuristic["_error"] = error
        return heuristic

    skill_out.setdefault("summary", "")
    return skill_out


# Silence unused-import warnings for symbols imported for side-effect reuse.
_ = (Any, InferredDependency)
