"""Domain starter-schema catalogs.

Exposes the YAML files under ``playbook/domain_catalogs/`` so the Product
Workbench wizard can offer domain-appropriate column suggestions when a
Product Owner is authoring a new spec. ``common.yaml`` holds cross-domain
canonicals (id, created_at, status enums) and is merged into every
catalog response.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session


from sqlmodel import select

from .. import embeddings
from ..config import BASE_DIR, PIPELINE_PLUGINS
from ..database import get_session
from ..models import AppSettings, Project
from ..neo4j_client import neo4j_session


router = APIRouter(prefix="/api/domain-catalogs", tags=["domain-catalogs"])


CATALOG_DIR = BASE_DIR / "playbook" / "domain_catalogs"
COMMON_FILE = "common.yaml"
ADVISOR_SKILL = "data-product-schema-advisor"
DISCOVERY_SKILL = "data-product-discovery-advisor"

# ── Discovery overlap tuning ─────────────────────────────────────────────────
# The "% of your intended columns" figure on the discovery screen is computed
# by SEMANTIC similarity (backend-owned, deterministic), not the LLM's old
# literal name intersection — so a candidate whose columns carry a prefix
# (hr_employee_id) or a synonym (worker_id) no longer reads 0% against the bare
# recommended catalog names. See _semantic_overlap / _token_overlap below.
_OVERLAP_SIM_THRESHOLD = 0.80    # cosine; a recommended col counts "present" if
                                 # its max cosine vs ANY candidate col >= this.
                                 # Starting point — NEEDS EMPIRICAL TUNING.
_OVERLAP_TOKEN_THRESHOLD = 0.50  # Jaccard threshold for the no-embeddings fallback.
_REUSE_OVERLAP_THRESHOLD = 80    # product verdict: reuse iff overlap_pct >= this
                                 # AND lifecycle is approved/published.
_MISSING_ATTR_CAP = 8            # max "Missing:" chips rendered per candidate.


def _load_yaml(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        raise HTTPException(404, f"Catalog {path.name} not found")
    if not isinstance(data, dict):
        raise HTTPException(500, f"Catalog {path.name} is not a YAML mapping")
    return data


def _normalise(entry: dict) -> dict:
    return {
        "name": entry.get("name", ""),
        "logical_type": entry.get("logical_type") or entry.get("logicalType", ""),
        "physical_type": entry.get("physical_type") or entry.get("physicalType", ""),
        "primary_key": bool(entry.get("primary_key") or entry.get("primaryKey", False)),
        "description": entry.get("description", ""),
        "recommended_rules": list(entry.get("recommended_rules") or entry.get("recommendedRules") or []),
        "source_category": entry.get("source_category", "domain"),
        # Structured PII/sensitivity classification (e.g. "pii"). Authoritative
        # signal the mask/reconciliation recommender (A1/A2/B) reads so PII
        # protection is domain-declared, not name-guessed. "none" when unset.
        "sensitivity": (entry.get("sensitivity") or "none"),
        # When set (e.g. "scd2"), this column only makes sense under a matching
        # :DatasetTransform.scd_policy. The schema advisor gates it out of
        # recommendations otherwise (see recommend_schema); the wizard renders a
        # "NEEDS SCD-2" chip if the PO picks it under a mismatched policy.
        "requires_scd_policy": entry.get("requires_scd_policy") or entry.get("requiresScdPolicy") or "",
    }


def _scd_requirements(domain: str) -> dict[str, str]:
    """Map column name -> required scd_policy ("scd2") for the given domain.

    Single source of truth for "which catalog columns are SCD-policy-gated",
    used by both `discover_schema` (drop them all — discovery has no policy
    context) and `recommend_schema` (drop those that mismatch the authored
    policy). Scans common + domain catalogs and is name-scoped, so it doesn't
    care which file's typing wins in get_catalog's dedup.
    """
    reqs: dict[str, str] = {}

    def _scan(cols: Any) -> None:
        for c in cols or []:
            if not isinstance(c, dict) or not c.get("name"):
                continue
            req = c.get("requires_scd_policy") or c.get("requiresScdPolicy")
            if req:
                reqs[c["name"]] = str(req)

    common_path = CATALOG_DIR / COMMON_FILE
    if common_path.is_file():
        _scan(_load_yaml(common_path).get("columns"))
    domain_path = CATALOG_DIR / f"{domain}.yaml"
    if domain_path.is_file():
        _scan(_load_yaml(domain_path).get("columns"))
    return reqs


def _label(data: dict[str, Any], stem: str) -> str:
    """Human display name for a domain — an explicit ``label:`` in the catalog
    YAML, else a title-cased fallback derived from the slug (so ``products_sales``
    → ``Products Sales`` and ``retail banking`` → ``Retail Banking``). Keeps the
    internal ``domain`` slug (used for path resolution + stored on products)
    decoupled from the proper name the wizard shows."""
    explicit = data.get("label")
    if explicit:
        return str(explicit)
    slug = data.get("domain") or stem
    return str(slug).replace("_", " ").replace("-", " ").title()


@router.get("")
def list_catalogs() -> dict[str, Any]:
    """Enumerate available domain catalogs with a column count per domain."""
    if not CATALOG_DIR.is_dir():
        return {"catalogs": []}
    catalogs = []
    for path in sorted(CATALOG_DIR.glob("*.yaml")):
        if path.name == COMMON_FILE:
            continue
        data = _load_yaml(path)
        cols = data.get("columns") or []
        catalogs.append({
            "domain": data.get("domain", path.stem),
            "label": _label(data, path.stem),
            "description": data.get("description", ""),
            "column_count": len(cols),
        })
    return {"catalogs": catalogs}


@router.get("/{domain}")
def get_catalog(domain: str) -> dict[str, Any]:
    """Return a domain catalog merged with the common canonical columns.

    Common columns come first (tagged ``source_category=common``) so wizard
    UIs can group them separately from the domain-specific starter set.
    """
    path = CATALOG_DIR / f"{domain}.yaml"
    if not path.is_file():
        raise HTTPException(404, f"No catalog for domain '{domain}'")
    data = _load_yaml(path)

    # The endpoint MUST NOT return two columns with the same `name`. The whole
    # consumer-wizard schema step keys off the physical name: selection is a
    # Set<name>, the row render is `key={c.name}`, and buildSpec emits one ODCS
    # property per row with `physicalName: name`. A duplicate name therefore
    # renders the column twice AND emits two same-physicalName properties (a
    # duplicate :DProdColumn) — the bug the PO sees as ghost rows that won't go
    # away when deselected. Domain catalogs legitimately re-declare names that
    # also live in common.yaml (e.g. the SCD-2 dating fields effective_from /
    # effective_to / is_current, or finance's fiscal_quarter), so we dedup here
    # rather than forbid the overlap in the YAML. The domain entry wins — a
    # domain catalog that redeclares a name is intentionally specialising it —
    # and we keep the FIRST position so common columns still lead the list.
    columns: list[dict] = []
    index_by_name: dict[str, int] = {}

    def _upsert(entry: dict) -> None:
        nm = entry["name"]
        if not nm:
            return
        if nm in index_by_name:
            # Override in place: a later definition (domain) supersedes an
            # earlier one (common) while preserving the original ordinal.
            prior = columns[index_by_name[nm]]
            # `requires_scd_policy` is a constraint on the column NAME, not its
            # typing — a domain catalog that retypes e.g. effective_to (date vs
            # timestamp) doesn't change that it's still SCD-2-only. Carry the
            # prior (common) constraint forward when the domain entry omits it,
            # so the advisor gate + the wizard's "NEEDS SCD-2" chip still fire.
            if prior.get("requires_scd_policy") and not entry.get("requires_scd_policy"):
                entry["requires_scd_policy"] = prior["requires_scd_policy"]
            columns[index_by_name[nm]] = entry
        else:
            index_by_name[nm] = len(columns)
            columns.append(entry)

    common_path = CATALOG_DIR / COMMON_FILE
    if common_path.is_file():
        common_data = _load_yaml(common_path)
        for c in (common_data.get("columns") or []):
            if not isinstance(c, dict):
                continue
            entry = _normalise(c)
            # Honour an explicit `source_category` on the YAML entry
            # (e.g. `derived` for the time-bucket / SCD-2-dating templates
            # in common.yaml). Otherwise default to `common` so legacy
            # entries without a category stay tagged correctly.
            if not c.get("source_category"):
                entry["source_category"] = "common"
            _upsert(entry)

    for c in (data.get("columns") or []):
        if not isinstance(c, dict):
            continue
        _upsert(_normalise(c))

    return {
        "domain": data.get("domain", domain),
        "label": _label(data, domain),
        "description": data.get("description", ""),
        "columns": columns,
    }


class ShapeContext(BaseModel):
    grain: str = ""
    filter: str = ""
    scd_policy: str = ""
    grouping_keys: list[str] = []


class RecommendRequest(BaseModel):
    idea: str
    name: str | None = None
    description: str | None = None
    purpose: str | None = None
    shape: ShapeContext | None = None
    # Contract IDs (`{project_code}-contract`) of source products the consumer
    # product will :CONSUMES. Used to fetch each source's :DProdColumn metadata
    # so the advisor can judge feasibility.
    source_contract_ids: list[str] = []


_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


# Token-budget caps for the per-source-product columns we ship to the advisor.
# Each column row is ~30 prompt tokens; 200 cols ≈ 6k tokens leaves room for
# the catalog YAML the skill `Read`s in (often 50-100 entries) plus envelope /
# shape prose without blowing past sensible input limits.
_MAX_COLS_PER_PRODUCT = 40
_MAX_COLS_TOTAL = 200


# Allowed feasibility enum values. Anything else is clamped to "sourced" so a
# misbehaving advisor doesn't break picker rendering.
_FEASIBILITY_VALUES = {"sourced", "derivable", "missing_source"}

# Allowed grain_alignment enum values. "finer" is a server-side classification
# the advisor isn't supposed to emit — finer-grain picks must not appear in
# recommended_columns at all. The frontend still renders a chip if such a row
# slips through (e.g. PO manually picks an order-grain column from the catalog
# in a customer-grain product), so we accept it as a legitimate value here.
_GRAIN_ALIGNMENT_VALUES = {"aligned", "coarser", "rollup_required", "finer", "unknown"}


def _parse_advisor_output(text: str) -> dict[str, Any]:
    """Pull the last fenced JSON block out of the advisor's transcript.

    The skill is instructed to emit exactly one fenced JSON block, but a
    defensive parser tolerates extra prose around it.
    """
    matches = _JSON_BLOCK_RE.findall(text or "")
    payload: dict[str, Any] = {}
    if matches:
        try:
            parsed = json.loads(matches[-1])
            if isinstance(parsed, dict):
                payload = parsed
        except json.JSONDecodeError:
            pass
    if not payload:
        # Fall back to scanning for a bare JSON object as a last resort.
        try:
            stripped = (text or "").strip()
            if stripped.startswith("{") and stripped.endswith("}"):
                parsed = json.loads(stripped)
                if isinstance(parsed, dict):
                    payload = parsed
        except json.JSONDecodeError:
            pass
    return payload


_SOURCE_COLUMNS_BY_CONTRACT_QUERY = """\
UNWIND $contract_ids AS cid
MATCH (dc:DataContract {id: cid})-[:MATERIALISES_AS]->(srcDp:DProdDataProduct)
MATCH (srcDp)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
      -[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
      -[:HAS_PRODUCT_COLUMN]->(dpc:DProdColumn)
RETURN cid                                AS contract_id,
       coalesce(srcDp.name, '')           AS product_name,
       coalesce(ods.physicalName, ods.name) AS dataset_name,
       dpc.name                           AS column_name,
       coalesce(dpc.dataType, '')         AS data_type,
       coalesce(dpc.description, '')      AS description,
       coalesce(dpc.sensitivity, 'none')  AS sensitivity,
       coalesce(ods.description, '')      AS table_description,
       coalesce(ods.relationshipKind, '') AS relationship_kind
ORDER BY contract_id, dataset_name, coalesce(dpc.ordinal, 9999)
"""


def _fetch_source_columns(session: Session, contract_ids: list[str]) -> list[dict[str, Any]]:
    """Pull the columns offered by the source products the consumer will
    :CONSUMES, grouped by source product.

    The consumer project may not exist yet (first-fire is pre-create), so this
    is keyed on the source-side ``DataContract.id`` instead of the consumer's
    ``project_code``. Returns a list shaped for the advisor's ``source_inputs``
    payload: ``[{contract_id, product_name, columns: [...], truncated}]``.

    Token-budget capping is applied here (per-product and overall) — descriptions
    win over name-only rows during truncation because that's what gives the
    advisor real semantic signal.

    Degrades gracefully on any Neo4j failure: logs and returns ``[]`` so the
    advisor still fires (without feasibility info) rather than 500ing the wizard.
    """
    if not contract_ids:
        return []
    settings = session.get(AppSettings, 1) or AppSettings()
    try:
        with neo4j_session(
            settings.neo4j_host, settings.neo4j_port,
            settings.neo4j_user, settings.neo4j_password, settings.neo4j_database,
        ) as ns:
            rows = [dict(r) for r in ns.run(
                _SOURCE_COLUMNS_BY_CONTRACT_QUERY,
                contract_ids=list(contract_ids),
            )]
    except Exception as e:
        import traceback
        print(f"[recommend] source-column fetch failed for contracts={contract_ids!r}: {e}")
        traceback.print_exc()
        return []

    by_product: dict[str, dict[str, Any]] = {}
    for row in rows:
        cid = row.get("contract_id") or ""
        if not cid:
            continue
        bucket = by_product.setdefault(cid, {
            "contract_id": cid,
            "product_name": row.get("product_name") or "",
            "columns": [],
            "_truncated": 0,
        })
        bucket["columns"].append({
            "name": row.get("column_name") or "",
            "dataset": row.get("dataset_name") or "",
            "data_type": row.get("data_type") or "",
            "description": row.get("description") or "",
            "sensitivity": row.get("sensitivity") or "none",
            "table_description": row.get("table_description") or "",
            "relationship_kind": row.get("relationship_kind") or "",
        })

    # Per-product cap. When trimming, columns with non-empty descriptions are
    # kept preferentially (described columns give the advisor better signal
    # than bare names).
    for bucket in by_product.values():
        cols = bucket["columns"]
        if len(cols) <= _MAX_COLS_PER_PRODUCT:
            continue
        described = [c for c in cols if c["description"]]
        bare = [c for c in cols if not c["description"]]
        kept: list[dict[str, Any]] = []
        for src in (described, bare):
            for c in src:
                if len(kept) >= _MAX_COLS_PER_PRODUCT:
                    break
                kept.append(c)
            if len(kept) >= _MAX_COLS_PER_PRODUCT:
                break
        bucket["_truncated"] = len(cols) - len(kept)
        bucket["columns"] = kept

    # Overall cap, applied across products in input order to keep first-listed
    # source products intact.
    out: list[dict[str, Any]] = []
    remaining = _MAX_COLS_TOTAL
    for cid in contract_ids:
        bucket = by_product.get(cid)
        if bucket is None:
            continue
        cols = bucket["columns"]
        if len(cols) > remaining:
            bucket["_truncated"] = bucket.get("_truncated", 0) + (len(cols) - remaining)
            bucket["columns"] = cols[:remaining]
        remaining -= len(bucket["columns"])
        out.append(bucket)
        if remaining <= 0:
            break

    return out


def _build_advisor_context(
    envelope: dict[str, Any] | None,
    shape: dict[str, Any] | None,
    source_inputs: list[dict[str, Any]],
) -> str:
    """Render the optional context fields into a compact JSON-ish block that's
    appended to the advisor's user prompt. Only emit sections that carry signal —
    an empty envelope/shape/source_inputs is omitted entirely so the first-fire
    prompt stays roughly identical to the pre-extension shape.
    """
    parts: list[str] = []

    if envelope:
        compact = {k: v for k, v in envelope.items() if v}
        if compact:
            parts.append(f"- envelope: {json.dumps(compact, ensure_ascii=False)}")

    if shape:
        compact = {k: v for k, v in shape.items() if v}
        if compact:
            parts.append(f"- shape: {json.dumps(compact, ensure_ascii=False)}")

    if source_inputs:
        # Strip the bookkeeping `_truncated` count out of the per-product block
        # before serialising — but mention truncation inline so the model knows
        # there are more columns than it can see.
        compact_inputs = []
        for src in source_inputs:
            entry: dict[str, Any] = {
                "contract_id": src.get("contract_id", ""),
                "product_name": src.get("product_name", ""),
                "columns": src.get("columns", []),
            }
            truncated = src.get("_truncated") or 0
            if truncated:
                entry["truncated_columns_omitted"] = truncated
            compact_inputs.append(entry)
        parts.append(f"- source_inputs: {json.dumps(compact_inputs, ensure_ascii=False)}")

    if not parts:
        return ""
    return "\n" + "\n".join(parts)


async def _run_schema_advisor(
    idea: str,
    domain: str,
    catalog_path: Path,
    envelope: dict[str, Any] | None = None,
    shape: dict[str, Any] | None = None,
    source_inputs: list[dict[str, Any]] | None = None,
    scd_gated_names: set[str] | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Invoke the data-product-schema-advisor skill via the Claude Code SDK.

    Returns ``(payload, error)`` where ``payload`` is the parsed JSON block
    (may include ``recommended_columns``, ``rationale``, ``column_details``)
    and ``error`` is ``None`` on success or a short string on SDK/parse
    failure (the wizard surfaces the banner).
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
        f"FIRST: Load the `{ADVISOR_SKILL}` skill via the Skill tool, then follow its "
        "instructions to the letter. Read the catalog YAML at the path the user gives "
        "you (and `playbook/domain_catalogs/common.yaml` if relevant), reason about the "
        "idea, and emit exactly one fenced JSON code block with `recommended_columns`, "
        "`rationale`, and (when context is provided) `column_details`. Do not write "
        "files. Do not run shell commands. Do not answer in prose outside the JSON block."
    )

    context_block = _build_advisor_context(envelope, shape, source_inputs or [])

    # Belt-and-suspenders grain directive. Lands in the user prompt verbatim
    # so even deployed skill versions that pre-date the SKILL.md grain rules
    # behave correctly. Only injected when the caller authored a non-empty
    # grain — empty-grain first-fires don't need this guidance.
    grain_directive = ""
    if shape and shape.get("grain"):
        declared_grain = str(shape["grain"]).strip()
        grain_directive = (
            f"\n\nHARD RULE — grain enforcement: declared grain is "
            f"`{declared_grain}`. A column whose source-table grain is finer "
            "than that grain (per-event / per-order / per-transaction when "
            "the declared grain is a dimension entity) MUST NOT appear in "
            "`recommended_columns` unless it is a roll-up measure with the "
            "aggregation declared in `why` (e.g. `SUM(total_amount) across "
            "orders`). For every entry in `column_details`, set "
            "`grain_alignment` to one of: `aligned` (lives at the declared "
            "grain), `coarser` (parent dimension that broadcasts cleanly), "
            "`rollup_required` (finer-grain measure surfaced as an aggregate "
            "— name the aggregation in `why`), or `unknown` (no signal). "
            "Never emit `finer` — exclude those columns instead."
        )

    # Belt-and-suspenders SCD directive. The SCD-2 dating columns
    # (effective_from / effective_to / is_current / expiration_*) only make
    # sense when history is preserved (scd_policy=scd2); under latest_only /
    # snapshot / none they're dead weight. Injected only when there's a real
    # mismatch — `scd_gated_names` is the catalog's scd-gated columns minus any
    # whose requirement matches the active policy, so an empty set (policy is
    # scd2, or no gated columns) emits nothing.
    scd_directive = ""
    if scd_gated_names:
        active_scd = (shape or {}).get("scd_policy") or "none"
        names = ", ".join(sorted(scd_gated_names))
        scd_directive = (
            f"\n\nHARD RULE — SCD policy: the declared scd_policy is "
            f"`{active_scd}` (not `scd2`). The SCD-2 history columns "
            f"({names}) only make sense when the consumer preserves history "
            "(scd_policy=scd2). They MUST NOT appear in `recommended_columns` "
            "or `column_details`."
        )

    user_prompt = (
        f"FIRST: Load the {ADVISOR_SKILL} skill using the Skill tool.\n\n"
        f"Recommend a curated subset of catalog columns for this idea.\n\n"
        f"Inputs:\n"
        f"- idea: {idea}\n"
        f"- domain: {domain}\n"
        f"- catalog_path: {catalog_path}"
        f"{context_block}"
        f"{grain_directive}"
        f"{scd_directive}\n\n"
        f"Read the catalog at `{catalog_path}` and `playbook/domain_catalogs/common.yaml` "
        "before responding. Output the single fenced JSON block as instructed by the skill."
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
                record_usage(source="schema_advisor", usage=extract_usage(message))
                if message.is_error:
                    return {}, "Schema advisor returned an error"
    except Exception as e:
        return {}, f"Schema advisor failed: {e}"

    payload = _parse_advisor_output("\n".join(transcript_parts))
    if not isinstance(payload, dict):
        return {}, "Advisor output not in the expected shape"
    return payload, None


def _sanitise_column_details(
    raw: Any,
    valid_names: set[str],
    shape_has_grain: bool = False,
) -> list[dict[str, Any]]:
    """Validate + clamp the advisor's ``column_details`` list. Drops entries
    whose ``name`` isn't in the final ``recommended_columns`` set, clamps
    ``relevance`` to ``[0,100]``, clamps ``feasibility`` and
    ``grain_alignment`` to their enums, coerces ``source_evidence`` to a list
    of strings, and caps ``why`` length so a chatty advisor can't push a
    5-paragraph rationale into the picker tooltip.

    ``shape_has_grain`` is True when the caller authored a non-empty
    ``shape.grain``. When False we force ``grain_alignment='aligned'`` on
    every entry so the first-fire shape-less path never renders chips.
    """
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or name not in valid_names or name in seen:
            continue
        seen.add(name)

        relevance = entry.get("relevance", 50)
        try:
            relevance_int = int(relevance)
        except (TypeError, ValueError):
            relevance_int = 50
        relevance_int = max(0, min(100, relevance_int))

        feasibility = entry.get("feasibility", "sourced")
        if not isinstance(feasibility, str) or feasibility not in _FEASIBILITY_VALUES:
            feasibility = "sourced"

        grain_alignment = entry.get("grain_alignment", "unknown")
        if not isinstance(grain_alignment, str) or grain_alignment not in _GRAIN_ALIGNMENT_VALUES:
            grain_alignment = "unknown"
        if not shape_has_grain:
            # No declared grain → no grain signal to show. Forcing 'aligned'
            # here keeps the frontend's chip render dormant on the shape-less
            # first-fire path.
            grain_alignment = "aligned"

        why = entry.get("why", "")
        if not isinstance(why, str):
            why = ""
        why = why.strip()
        # Cap at ~240 chars (~one sentence + slack). Defensive — the skill is
        # told one sentence, but a 5-paragraph response would balloon the
        # response and break the tooltip layout.
        if len(why) > 240:
            why = why[:237].rstrip() + "..."

        evidence_raw = entry.get("source_evidence") or []
        if isinstance(evidence_raw, list):
            evidence = [str(e) for e in evidence_raw if isinstance(e, (str, int))][:8]
        else:
            evidence = []

        out.append({
            "name": name,
            "relevance": relevance_int,
            "why": why,
            "feasibility": feasibility,
            "grain_alignment": grain_alignment,
            "source_evidence": evidence,
        })
    return out


@router.post("/{domain}/recommend")
async def recommend_schema(
    domain: str,
    body: RecommendRequest,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Run the schema-advisor skill against a domain catalog and return a
    curated subset of catalog column names matched to the user's idea +
    description envelope + dataset shape, annotated with per-column
    relevance / feasibility against the source products the consumer will
    :CONSUMES.

    Fires twice in the wizard: once on the domain-selected transition (with
    most context fields empty — basic catalog filter) and once on entry to
    the schema picker step after Shape + Source Inputs are authored (full
    context — ranked + feasibility-aware).
    """
    idea = (body.idea or "").strip()
    if not idea:
        raise HTTPException(400, "idea is required")

    catalog_path = CATALOG_DIR / f"{domain}.yaml"
    if not catalog_path.is_file():
        raise HTTPException(404, f"No catalog for domain '{domain}'")

    catalog_data = _load_yaml(catalog_path)
    catalog_names: set[str] = {
        c.get("name") for c in (catalog_data.get("columns") or [])
        if isinstance(c, dict) and c.get("name")
    }
    common_path = CATALOG_DIR / COMMON_FILE
    if common_path.is_file():
        for c in (_load_yaml(common_path).get("columns") or []):
            if isinstance(c, dict) and c.get("name"):
                catalog_names.add(c["name"])
    # name -> required scd_policy ("scd2") for columns that only make sense
    # under a matching :DatasetTransform.scd_policy. Shared with discover_schema
    # so the two endpoints can't drift on what counts as policy-gated.
    scd_requirements = _scd_requirements(domain)

    envelope: dict[str, Any] = {}
    if body.name:
        envelope["name"] = body.name
    if body.description:
        envelope["description"] = body.description
    if body.purpose:
        envelope["purpose"] = body.purpose

    shape: dict[str, Any] = {}
    if body.shape is not None:
        if body.shape.grain:
            shape["grain"] = body.shape.grain
        if body.shape.filter:
            shape["filter"] = body.shape.filter
        if body.shape.scd_policy:
            shape["scd_policy"] = body.shape.scd_policy
        if body.shape.grouping_keys:
            shape["grouping_keys"] = list(body.shape.grouping_keys)

    source_inputs = _fetch_source_columns(session, body.source_contract_ids)

    # Columns whose declared scd_policy requirement doesn't match the policy the
    # PO authored in the Shape step — these must not be auto-recommended (e.g.
    # effective_from/effective_to/is_current under latest_only / snapshot / none).
    active_scd = shape.get("scd_policy") or ""
    scd_gated_names = {nm for nm, req in scd_requirements.items() if req != active_scd}

    payload, error = await _run_schema_advisor(
        idea,
        domain,
        catalog_path,
        envelope=envelope or None,
        shape=shape or None,
        source_inputs=source_inputs,
        scd_gated_names=scd_gated_names,
    )

    raw_cols = payload.get("recommended_columns") if isinstance(payload, dict) else None
    if not isinstance(raw_cols, list):
        raw_cols = []
    columns = [str(c) for c in raw_cols if isinstance(c, (str, int))]
    rationale = payload.get("rationale") if isinstance(payload, dict) else ""
    rationale = str(rationale) if isinstance(rationale, str) else ""

    valid = [c for c in columns if c in catalog_names]
    dropped = len(columns) - len(valid)

    shape_has_grain = bool(shape.get("grain"))
    column_details = _sanitise_column_details(
        payload.get("column_details") if isinstance(payload, dict) else None,
        set(valid),
        shape_has_grain=shape_has_grain,
    )

    # Safety net: when the PO declared a grain, drop any pick the advisor
    # marked finer-grain. The frontend still renders a chip if the PO later
    # picks a finer-grain column manually from the catalog browser — this
    # only filters the advisor's own slip-ups (e.g. order_id leaking into a
    # customer-grain product). Counted into ``dropped`` so the UI banner
    # still surfaces a number when relevant.
    if shape_has_grain:
        # Drop only columns the advisor EXPLICITLY marked finer-grain. Using the
        # surviving-detail names as a whole allowlist would also drop valid
        # picks that simply have no detail entry (column_details can be a
        # subset of ``valid``).
        finer_names = {
            d.get("name")
            for d in column_details
            if d.get("grain_alignment") == "finer" and d.get("name")
        }
        if finer_names:
            valid = [c for c in valid if c not in finer_names]
            column_details = [d for d in column_details if d.get("name") not in finer_names]
            dropped += len(finer_names)

    # Safety net: drop SCD-policy-gated columns the advisor recommended anyway
    # (e.g. effective_from under latest_only). Mirrors the grain filter — the
    # frontend still renders a "NEEDS SCD-2" chip if the PO picks one manually;
    # this only filters the advisor's own slip-ups. Counted into ``dropped``.
    if scd_gated_names:
        kept = [c for c in valid if c not in scd_gated_names]
        scd_filtered = len(valid) - len(kept)
        if scd_filtered:
            valid = kept
            column_details = [d for d in column_details if d["name"] not in scd_gated_names]
            dropped += scd_filtered

    return {
        "columns": valid,
        "rationale": rationale,
        "dropped": dropped,
        "column_details": column_details,
        "source_input_count": len(source_inputs),
        "source_column_count": sum(len(s.get("columns") or []) for s in source_inputs),
        "error": error,
    }


# ── Discovery: similar products + matching templates + recommended columns ─

# Cypher for cross-project candidate products in the chosen domain. Mirrors
# the marketplace ALL_PRODUCTS shape but filters by domain and excludes
# rejected/superseded contracts so we never recommend "reuse" of dead work.
_CANDIDATE_PRODUCTS_QUERY = """\
MATCH (dc:DataContract)
WHERE toLower(coalesce(dc.domain, '')) = toLower($domain)
  AND NOT dc:ProductTemplate
  AND NOT (coalesce(dc.currentLifecycleState, 'draft') IN ['rejected', 'superseded'])
OPTIONAL MATCH (dc)-[:MATERIALISES_AS]->(dp:DProdDataProduct)
OPTIONAL MATCH (dp)-[:DPROD_OUTPUT_PORT]->()-[:DPROD_OUTPUT_DATASET]->()
      -[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
WITH dc, dp,
     count(DISTINCT pc) AS column_count,
     collect(DISTINCT pc.name) AS column_names
OPTIONAL MATCH (dc)-[:HAS_OWNER]->(owner:DataContractOwner)
WITH dc, dp, column_count, column_names, collect(owner)[0] AS first_owner
RETURN
    coalesce(dp.uri, dc.id) AS uri,
    dp.uri AS dp_uri,
    dc.id AS contract_id,
    coalesce(dc.name, dp.name, dc.dataProduct, '') AS name,
    coalesce(dc.description, '') AS description,
    coalesce(dc.purpose, '') AS purpose,
    coalesce(dc.domain, '') AS domain,
    coalesce(dc.currentLifecycleState, 'draft') AS lifecycle_state,
    coalesce(first_owner.email, '') AS owner_email,
    column_count,
    [n IN column_names WHERE n IS NOT NULL] AS column_names
ORDER BY
    CASE coalesce(dc.currentLifecycleState, 'draft')
        WHEN 'published' THEN 0
        WHEN 'approved' THEN 1
        WHEN 'in_engineering' THEN 2
        WHEN 'submitted' THEN 3
        ELSE 4
    END,
    name
LIMIT 30
"""


def _fetch_candidate_products(session: Session, domain: str) -> list[dict]:
    """Pull cross-project DataContracts in the chosen domain. Empty list on
    any Neo4j failure — discovery degrades gracefully to template + columns
    rather than 500-ing the wizard.

    Each row is enriched with ``project_id`` (resolved from contract_id via
    the same ``{project_code}-contract`` convention as the marketplace) so
    the wizard can deep-link back into ``/product/edit/{id}`` for in-progress
    drafts owned by the current user."""
    settings = session.get(AppSettings, 1) or AppSettings()
    try:
        with neo4j_session(
            settings.neo4j_host, settings.neo4j_port,
            settings.neo4j_user, settings.neo4j_password, settings.neo4j_database,
        ) as ns:
            rows = [dict(r) for r in ns.run(_CANDIDATE_PRODUCTS_QUERY, domain=domain)]
    except Exception as e:
        # Log but don't fail the whole endpoint — discovery degrades to
        # template + columns when the graph is unreachable.
        import traceback
        print(f"[discover] candidate fetch failed for domain={domain!r}: {e}")
        traceback.print_exc()
        return []
    for row in rows:
        contract_id = row.get("contract_id") or ""
        if contract_id.endswith("-contract"):
            project_code = contract_id[: -len("-contract")]
            project = session.exec(select(Project).where(Project.project_code == project_code)).first()
            row["project_id"] = project.id if project else None
        else:
            row["project_id"] = None
    return rows


# (The legacy file-based `_load_template` was retired with the
# `playbook/odcs_templates` corpus — candidate templates now come from the
# Blueprint Library via `_library_candidate_templates`.)


def _library_candidate_templates(session, domain: str) -> list[dict]:
    """PUBLISHED Blueprint-Library templates whose domain matches — the single
    source of truth. Returns the `{template_id, name, description, purpose,
    domain, column_count, column_names, _raw}` shape the advisor wants."""
    try:
        from .. import template_store
        rows = template_store.list_templates_raw(session)
    except Exception:
        return []
    out: list[dict] = []
    for r in rows:
        if r.get("status") != "published":
            continue
        if (r.get("domain") or "").lower() != domain.lower():
            continue
        spec = template_store._read_template_from_graph(r["id"], session)
        if not spec:
            continue
        raw = {k: v for k, v in spec.items() if k not in template_store._TEMPLATE_META_KEYS}
        column_names: list[str] = []
        for ds in (raw.get("schema") or []):
            for prop in (ds.get("properties") or []):
                if isinstance(prop, dict):
                    nm = prop.get("name") or prop.get("physicalName")
                    if nm:
                        column_names.append(str(nm))
        out.append({
            "template_id": r["id"],
            "name": raw.get("name", ""),
            "description": raw.get("description", ""),
            "purpose": raw.get("purpose", ""),
            "domain": raw.get("domain", ""),
            "column_count": len(column_names),
            "column_names": column_names,
            "_raw": raw,
        })
    return out


def _fetch_candidate_templates(session, domain: str) -> list[dict]:
    """List published Blueprint-Library templates whose ``domain`` matches — the
    single source of truth (the legacy ``playbook/odcs_templates`` files were
    retired once the Library seeds shipped)."""
    return _library_candidate_templates(session, domain)


async def _run_discovery_advisor(
    idea: str,
    domain: str,
    catalog_path: Path,
    candidates: list[dict],
    templates: list[dict],
) -> tuple[dict, str | None]:
    """Invoke the discovery skill. Returns ``(payload, error)`` where payload
    is the parsed JSON block (possibly empty) and error is None on success or
    a short string on SDK/parse failure (the wizard surfaces the banner)."""
    try:
        from claude_agent_sdk import query, ClaudeAgentOptions
        from claude_agent_sdk.types import (
            AssistantMessage,
            ResultMessage,
            TextBlock,
        )
    except ImportError:
        return {}, "claude-agent-sdk is not installed"

    # Strip the raw template YAML — the skill only needs the summary fields
    # listed in its SKILL.md. Keeping _raw out of the prompt keeps it small.
    skill_templates = [
        {k: v for k, v in t.items() if k != "_raw"} for t in templates
    ]
    # The skill cares about name/description/purpose/lifecycle/owner/column_count
    # plus column_names — the names still inform its match_score/delta judgment
    # (overlap_pct/missing_attributes are now computed backend-side, not here);
    # backend-only fields (dp_uri, contract_id, project_id) just bloat the prompt.
    _SKILL_FIELDS = {
        "uri", "name", "description", "purpose",
        "domain", "lifecycle_state", "owner_email",
        "column_count", "column_names",
    }
    skill_candidates = [
        {k: v for k, v in c.items() if k in _SKILL_FIELDS} for c in candidates
    ]

    system_prompt = (
        f"FIRST: Load the `{DISCOVERY_SKILL}` skill via the Skill tool, then follow its "
        "instructions to the letter. Read the catalog YAML at the path the user gives "
        "you (and `playbook/domain_catalogs/common.yaml` if relevant), reason about the "
        "idea against the existing products and templates already provided in the prompt, "
        "and emit exactly one fenced JSON code block with `similar_products`, "
        "`matching_templates`, `recommended_columns`, and `rationale` keys. Do not write "
        "files. Do not run shell commands. Do not invent URIs, template IDs, or column "
        "names. Do not answer in prose outside the JSON block."
    )

    user_prompt = (
        f"FIRST: Load the {DISCOVERY_SKILL} skill using the Skill tool.\n\n"
        f"Rank existing products and templates against this idea and pick a starter column set.\n\n"
        f"Inputs:\n"
        f"- idea: {idea}\n"
        f"- domain: {domain}\n"
        f"- catalog_path: {catalog_path}\n\n"
        f"existing_products (JSON):\n```json\n{json.dumps(skill_candidates)}\n```\n\n"
        f"templates (JSON):\n```json\n{json.dumps(skill_templates)}\n```\n\n"
        f"Read the catalog at `{catalog_path}` and `playbook/domain_catalogs/common.yaml` "
        "before responding. Output the single fenced JSON block as instructed by the skill."
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
                record_usage(source="discovery_advisor", usage=extract_usage(message))
                if message.is_error:
                    return {}, "Discovery advisor returned an error"
    except Exception as e:
        return {}, f"Discovery advisor failed: {e}"

    payload = _parse_advisor_output("\n".join(transcript_parts))
    if not isinstance(payload, dict):
        return {}, "Advisor output not in the expected shape"
    return payload, None


# ── Semantic column-overlap (backend-owned; replaces the LLM's literal math) ─
#
# "% of your intended columns" and the "Missing:" chips are computed here from
# the recommended catalog columns vs. a candidate product/template's actual
# column names — by cosine similarity over local embeddings when available, else
# a normalized-token Jaccard fallback. Both are deterministic, so the number is
# trustworthy (the LLM used to do the arithmetic with no guarantee) and prefix /
# synonym matches count instead of reading 0%.


def _cosine(a: list[float], b: list[float]) -> float:
    """Pure cosine similarity. 0.0 on empty / mismatched vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _normalize_column_name(name: str) -> str:
    """snake_case / camelCase / PascalCase / kebab → lowercased, space-separated
    tokens: ``hr_employee_id`` → ``hr employee id``, ``employeeId`` → ``employee
    id``, ``EmployeeID`` → ``employee id``. bge is trained on natural language,
    not identifiers, so this lifts embedding quality AND feeds the token
    fallback."""
    s = name or ""
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s)     # camelCase boundary
    s = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", s)   # ACRONYMWord boundary
    s = re.sub(r"[_\-]+", " ", s)                      # snake / kebab
    s = re.sub(r"\s+", " ", s)
    return s.strip().lower()


def _semantic_overlap(
    recommended: list[str],
    candidate_columns: list[str],
    vectors: dict[str, list[float]],
    threshold: float,
) -> tuple[int, list[str]]:
    """Coverage metric (NOT a bijection): for each recommended column take its
    max cosine against ANY candidate column; count it "present" if
    ``>= threshold`` else "missing". Returns ``(overlap_pct, missing)`` where
    ``missing`` preserves ``recommended`` order and is capped at
    ``_MISSING_ATTR_CAP``. Empty ``recommended`` → ``(0, [])``."""
    if not recommended:
        return 0, []
    cand_vecs = [vectors.get(_normalize_column_name(c)) for c in candidate_columns]
    cand_vecs = [v for v in cand_vecs if v]
    present = 0
    missing: list[str] = []
    for rec in recommended:
        rv = vectors.get(_normalize_column_name(rec))
        best = max((_cosine(rv, cv) for cv in cand_vecs), default=0.0) if rv else 0.0
        if best >= threshold:
            present += 1
        elif len(missing) < _MISSING_ATTR_CAP:
            missing.append(rec)
    return round(100 * present / len(recommended)), missing


def _token_overlap(
    recommended: list[str],
    candidate_columns: list[str],
    threshold: float,
) -> tuple[int, list[str]]:
    """Deterministic no-embeddings fallback: normalized-token **Jaccard** (not
    raw subset, so a bare ``id`` can't match everything). Still strictly better
    than the old literal intersection — ``employee_id`` vs ``hr_employee_id`` is
    Jaccard 0.67 (present) versus a literal 0. Same shape as
    :func:`_semantic_overlap`."""
    if not recommended:
        return 0, []
    cand_toks = [set(_normalize_column_name(c).split()) for c in candidate_columns]
    present = 0
    missing: list[str] = []
    for rec in recommended:
        rt = set(_normalize_column_name(rec).split())
        best = 0.0
        for ct in cand_toks:
            if rt and ct:
                best = max(best, len(rt & ct) / len(rt | ct))
        if best >= threshold:
            present += 1
        elif len(missing) < _MISSING_ATTR_CAP:
            missing.append(rec)
    return round(100 * present / len(recommended)), missing


def _embed_column_names(names: list[str]) -> dict[str, list[float]]:
    """Embed the DEDUPED UNION of normalized column names in one batch and
    return ``{normalized_name: vector}``. Symmetric (``embed_documents`` for both
    sides — no bge query prefix — since both are short column-name phrases of the
    same kind, which also lets everything share one batch). Returns ``{}`` when
    embeddings are unavailable / fail / length-mismatch → caller uses the token
    fallback. Never raises into the request path."""
    if not embeddings.available():
        return {}
    uniq = list(dict.fromkeys(
        _normalize_column_name(n) for n in names if n and n.strip()
    ))
    if not uniq:
        return {}
    vecs = embeddings.embed_documents(uniq)   # [] on failure
    if len(vecs) != len(uniq):
        return {}
    return dict(zip(uniq, vecs))


def _derive_product_verdict(overlap_pct: int, lifecycle_state: str) -> str:
    """reuse iff column coverage is high AND the product is finished; else
    extend (the safer default — it never suggests abandoning authoring).
    Templates don't use this — they're always ``clone``."""
    if overlap_pct >= _REUSE_OVERLAP_THRESHOLD and (lifecycle_state or "").lower() in {"approved", "published"}:
        return "reuse"
    return "extend"


def _shape_template_for_response(template: dict) -> dict:
    """Drop internal-only fields before returning the template to the
    frontend. The clone-into-wizard path needs ``column_names`` and the raw
    spec, so those stay; ``_raw`` is the parsed YAML used by the wizard's
    template-clone handler to populate name / description / purpose."""
    out = {k: v for k, v in template.items() if k != "_raw"}
    out["spec"] = template.get("_raw", {})
    return out


@router.post("/{domain}/discover")
async def discover_schema(
    domain: str,
    body: RecommendRequest,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Discovery endpoint for the wizard transition between Step 1 and Step 2.

    Pre-fetches similar published / in-progress products from the cross-project
    graph and matching ODCS templates from ``playbook/odcs_templates``, then
    asks the discovery skill to rank them against the idea and pick a starter
    column set. The wizard renders three sections from the response (similar
    products / matching templates / recommended columns) and falls through to
    Step 2 with the recommended columns when both the upper sections are
    empty.
    """
    idea = (body.idea or "").strip()
    if not idea:
        raise HTTPException(400, "idea is required")

    catalog_path = CATALOG_DIR / f"{domain}.yaml"
    if not catalog_path.is_file():
        raise HTTPException(404, f"No catalog for domain '{domain}'")

    catalog_data = _load_yaml(catalog_path)
    catalog_names = {
        c.get("name") for c in (catalog_data.get("columns") or [])
        if isinstance(c, dict) and c.get("name")
    }
    common_path = CATALOG_DIR / COMMON_FILE
    if common_path.is_file():
        common_data = _load_yaml(common_path)
        for c in (common_data.get("columns") or []):
            if isinstance(c, dict) and c.get("name"):
                catalog_names.add(c["name"])

    candidates = _fetch_candidate_products(session, domain)
    templates = _fetch_candidate_templates(session, domain)
    candidate_uris = {c.get("uri") for c in candidates if c.get("uri")}
    template_ids = {t["template_id"]: t for t in templates}

    payload, error = await _run_discovery_advisor(idea, domain, catalog_path, candidates, templates)

    # Validate the skill's picks against the candidate sets and the catalog.
    raw_similar = payload.get("similar_products") or []
    raw_templates = payload.get("matching_templates") or []
    raw_cols = payload.get("recommended_columns") or []
    rationale = payload.get("rationale") or ""

    raw_col_strs = [str(c) for c in raw_cols if isinstance(c, (str, int))]
    valid_cols = [c for c in raw_col_strs if c in catalog_names]
    # Discovery runs before the Shape step, so scd_policy is unknown here. Drop
    # any SCD-policy-gated column (e.g. effective_from / effective_to /
    # is_current) — auto-selecting them now would land them in the wizard's
    # selectedColumns before the PO has chosen a policy, and they'd survive the
    # union-only step 3→4 merge even under latest_only. The policy-aware
    # /recommend pass adds them back when (and only when) the PO picks scd2.
    scd_gated = set(_scd_requirements(domain))
    if scd_gated:
        valid_cols = [c for c in valid_cols if c not in scd_gated]
    dropped = len(raw_col_strs) - len(valid_cols)

    candidates_by_uri = {c["uri"]: c for c in candidates if c.get("uri")}

    # Semantic overlap is backend-owned: build one embedding index over the
    # recommended columns + every candidate/template column name, then score each
    # candidate against it. `overlap_vectors == {}` (embeddings unavailable) falls
    # through to the deterministic token Jaccard. The LLM's overlap_pct/
    # missing_attributes/verdict are now ignored — only match_score/delta remain.
    _overlap_names = list(valid_cols)
    for _c in candidates:
        _overlap_names += _c.get("column_names") or []
    for _t in templates:
        _overlap_names += _t.get("column_names") or []
    overlap_vectors = _embed_column_names(_overlap_names)

    def _overlap(cols: list[str]) -> tuple[int, list[str]]:
        if overlap_vectors:
            return _semantic_overlap(valid_cols, cols, overlap_vectors, _OVERLAP_SIM_THRESHOLD)
        return _token_overlap(valid_cols, cols, _OVERLAP_TOKEN_THRESHOLD)

    similar_products: list[dict] = []
    if isinstance(raw_similar, list):
        for entry in raw_similar:
            if not isinstance(entry, dict):
                continue
            uri = entry.get("uri")
            if uri not in candidate_uris:
                continue
            base = candidates_by_uri[uri]
            overlap_pct, missing = _overlap(base.get("column_names") or [])
            similar_products.append({
                **base,
                "match_score": entry.get("match_score"),
                "delta": entry.get("delta", ""),
                "verdict": _derive_product_verdict(overlap_pct, base.get("lifecycle_state", "")),
                "overlap_pct": overlap_pct,
                "missing_attributes": missing,
            })

    matching_templates: list[dict] = []
    if isinstance(raw_templates, list):
        for entry in raw_templates:
            if not isinstance(entry, dict):
                continue
            tid = entry.get("template_id")
            if tid not in template_ids:
                continue
            base = _shape_template_for_response(template_ids[tid])
            overlap_pct, missing = _overlap(base.get("column_names") or [])
            matching_templates.append({
                **base,
                "match_score": entry.get("match_score"),
                "delta": entry.get("delta", ""),
                "verdict": "clone",  # Templates always clone — UI relies on this.
                "overlap_pct": overlap_pct,
                "missing_attributes": missing,
            })

    return {
        "similar_products": similar_products,
        "matching_templates": matching_templates,
        "recommended_columns": valid_cols,
        "rationale": str(rationale),
        "dropped": dropped,
        "error": error,
    }
