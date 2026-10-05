"""Cross-product semantic-layer recommender — Phase 4 of the deployment_preview_semantic_layer roadmap.

Walks the project portfolio (all `:Project`s with a contract that has at
least one `:DProdColumn`), assembles a cross-product graph snapshot,
hands it to the `business-concept-advisor` skill, and persists ranked
candidate `:BusinessConceptRecommendation` nodes.

v1 is read-only: candidates are recommendations, not concepts. The
Steward triages them via the SemanticRecommenderPage (Reject / Edit /
Export YAML). No `:BusinessConcept` nodes are created here; that is a
deliberately-deferred next phase per the design doc.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml

from .config import BASE_DIR, PIPELINE_PLUGINS
from .models import AppSettings
from .neo4j_client import neo4j_session


ADVISOR_SKILL = "business-concept-advisor"
ADVISOR_TIMEOUT_SECONDS = 120
EVALUATOR_VERSION = "concept-advisor-v0.1.0"

MAX_PRODUCTS_IN_PROMPT = 12  # cap so a 30-product portfolio doesn't blow the prompt
MAX_COLUMNS_PER_DATASET = 60  # cap per dataset
_MAX_INPUT_JSON_CHARS = 24_000


# ── Cross-project graph snapshot ───────────────────────────────────────────


_PORTFOLIO_QUERY = """
MATCH (p:Project)-[:HAS_CONTRACT]->(dc:DataContract)
WHERE coalesce(dc.isCurrent, true) = true
MATCH (dc)-[:MATERIALISES_AS]->(dp:DProdDataProduct)
OPTIONAL MATCH (dp)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
    -[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
OPTIONAL MATCH (ods)-[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
// Bridge to operational :Column via the mapping to pull profiling top-values.
// Each DProdColumn may have multiple mappings → take the first non-empty.
OPTIONAL MATCH (cm:ColumnMapping)-[:MAPS_TO_PRODUCT_COLUMN]->(pc)
OPTIONAL MATCH (cm)-[:MAPS_SOURCE_COLUMN]->(src:Column)
OPTIONAL MATCH (src)-[:HAS_TOP_VALUE]->(tv:TopValue)
// Consumer-upstream mapping: the source is a :DProdColumn (another product's
// column), which carries no profiling top-values itself. Walk one mapping hop
// further up to ITS own source :Column so a consumer-on-source mapping isn't
// silently dropped from the profiling signal.
OPTIONAL MATCH (cm)-[:MAPS_SOURCE_COLUMN]->(:DProdColumn)
    <-[:MAPS_TO_PRODUCT_COLUMN]-(:ColumnMapping)-[:MAPS_SOURCE_COLUMN]->(up_src:Column)
OPTIONAL MATCH (up_src)-[:HAS_TOP_VALUE]->(tv2:TopValue)
WITH p, dc, dp, ods, pc,
     (collect(DISTINCT tv.value) + collect(DISTINCT tv2.value))[..10] AS top_values
WITH p, dc, dp, ods, pc, top_values
ORDER BY p.projectCode, ods.physicalName, pc.ordinal
WITH p, dc, dp, ods,
     collect(CASE WHEN pc IS NULL THEN NULL ELSE {
       column_uri: pc.uri,
       name: pc.name,
       data_type: coalesce(pc.dataType, ''),
       is_primary_key: coalesce(pc.isPrimaryKey, false),
       description: coalesce(pc.description, ''),
       top_values: top_values
     } END) AS pcs_raw
WITH p, dc, dp,
     collect(CASE WHEN ods IS NULL THEN NULL ELSE {
       uri: ods.uri,
       name: coalesce(ods.name, ''),
       physical_name: ods.physicalName,
       relationship_kind: coalesce(ods.relationshipKind, 'unknown'),
       description: coalesce(ods.description, ''),
       columns: [c IN pcs_raw WHERE c IS NOT NULL]
     } END) AS datasets_raw
RETURN p.projectCode AS project_code,
       coalesce(p.domain, '') AS domain,
       dc.id AS contract_id,
       dp.uri AS product_uri,
       coalesce(dp.name, dc.name, '') AS product_name,
       coalesce(dp.productKind, '') AS product_kind,
       coalesce(dc.description, '') AS description,
       [d IN datasets_raw WHERE d IS NOT NULL] AS datasets
"""


_OSI_MODELS_QUERY = """
MATCH (dc:DataContract)-[:HAS_OSI_EVAL]->(oe:OsiEvaluation)
WHERE oe.semanticModelJson IS NOT NULL AND oe.semanticModelJson <> ''
WITH dc, oe ORDER BY oe.evaluatedAt DESC
WITH dc, head(collect(oe.semanticModelJson)) AS model_json
WHERE model_json IS NOT NULL
RETURN dc.id AS contract_id, model_json
"""


def _load_domain_catalogs() -> list[dict[str, Any]]:
    """Load all playbook/domain_catalogs/*.yaml files. Each contributes
    a (domain, canonical_columns, keywords) entry the skill can use as
    domain-knowledge ground truth."""
    cats_dir = BASE_DIR / "playbook" / "domain_catalogs"
    out: list[dict[str, Any]] = []
    if not cats_dir.exists():
        return out
    for p in sorted(cats_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(p.read_text())
        except (yaml.YAMLError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        domain = data.get("domain") or p.stem
        cols = data.get("columns") or []
        names = [c.get("name") for c in cols if isinstance(c, dict) and c.get("name")]
        # Distill keywords from descriptions: pull every word ≥4 chars,
        # lowercase, deduped. This is rough; the skill uses these as hints,
        # not as a strict allow-list.
        words: set[str] = set()
        for c in cols:
            if not isinstance(c, dict):
                continue
            desc = (c.get("description") or "").lower()
            for w in re.findall(r"[a-z][a-z_]{3,}", desc):
                words.add(w)
        # Cap to keep prompt size bounded.
        out.append({
            "domain": domain,
            "canonical_columns": names[:50],
            "keywords": sorted(words)[:30],
        })
    return out


@dataclass
class RecommenderInputs:
    products: list[dict[str, Any]]
    domain_catalogs: list[dict[str, Any]]
    osi_models: list[dict[str, Any]]


def gather_inputs(neo4j_settings: AppSettings) -> RecommenderInputs:
    """Walk the cross-project graph for the recommender. Read-only; the
    only legitimate cross-project read in the system per the design doc."""
    with neo4j_session(
        neo4j_settings.neo4j_host, neo4j_settings.neo4j_port,
        neo4j_settings.neo4j_user, neo4j_settings.neo4j_password, neo4j_settings.neo4j_database,
    ) as ns:
        portfolio_rows = list(ns.run(_PORTFOLIO_QUERY))
        osi_rows = list(ns.run(_OSI_MODELS_QUERY))

    products: list[dict[str, Any]] = []
    for r in portfolio_rows:
        rd = dict(r)
        # Skip products with no columns at all — they contribute nothing.
        datasets = rd.get("datasets") or []
        total_cols = sum(len(d.get("columns") or []) for d in datasets)
        if total_cols == 0:
            continue
        # Cap columns per dataset to bound prompt size.
        capped_datasets = []
        for d in datasets:
            d = dict(d)
            cols = d.get("columns") or []
            d["columns"] = cols[:MAX_COLUMNS_PER_DATASET]
            capped_datasets.append(d)
        rd["datasets"] = capped_datasets
        products.append(rd)

    # Sort: more columns first (richer evidence), then alphabetical for
    # deterministic test output.
    products.sort(
        key=lambda p: (
            -sum(len(d.get("columns") or []) for d in p.get("datasets") or []),
            p.get("project_code") or "",
        )
    )
    products = products[:MAX_PRODUCTS_IN_PROMPT]

    osi_models: list[dict[str, Any]] = []
    for r in osi_rows:
        rd = dict(r)
        try:
            model = json.loads(rd.get("model_json") or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(model, dict):
            continue
        sm = model.get("semantic_model") or []
        if not sm:
            continue
        first = sm[0] if isinstance(sm, list) else {}
        osi_models.append({
            "contract_id": rd.get("contract_id"),
            "metrics": first.get("metrics") or [],
            "relationships": first.get("relationships") or [],
            "ai_context": first.get("ai_context") or {},
        })

    return RecommenderInputs(
        products=products,
        domain_catalogs=_load_domain_catalogs(),
        osi_models=osi_models,
    )


# ── Advisor (LLM) ──────────────────────────────────────────────────────────


_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


@dataclass
class AdvisorPayload:
    narrative: str = ""
    concepts: list[dict[str, Any]] = None  # type: ignore[assignment]


def _parse_advisor_payload(text: str) -> AdvisorPayload:
    out = AdvisorPayload(narrative="", concepts=[])
    matches = _JSON_BLOCK_RE.findall(text or "")
    if not matches:
        return out
    try:
        parsed = json.loads(matches[-1])
    except json.JSONDecodeError:
        return out
    if not isinstance(parsed, dict):
        return out
    if isinstance(parsed.get("narrative"), str):
        out.narrative = parsed["narrative"]
    if isinstance(parsed.get("concepts"), list):
        out.concepts = [c for c in parsed["concepts"] if isinstance(c, dict)]
    return out


def _validate_concepts(
    concepts: list[dict[str, Any]], known_column_uris: set[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Drop concepts that lack a real evidence_column citation (URI in
    the input). Drop evidence_column entries whose URI isn't in our
    known set. Same defensive gate as the OSI advisor / reflector."""
    warnings: list[str] = []
    cleaned: list[dict[str, Any]] = []
    for c in concepts:
        name = c.get("concept_name") or "?"
        evidence = c.get("evidence_columns") or []
        kept = []
        for ev in evidence:
            if not isinstance(ev, dict):
                continue
            uri = ev.get("column_uri")
            if not uri:
                continue
            if uri not in known_column_uris:
                warnings.append(f"{name}: evidence_column URI not in input: {uri}")
                continue
            kept.append(ev)
        if not kept:
            warnings.append(f"{name}: dropped — no valid evidence_columns")
            continue
        cleaned_c = dict(c)
        cleaned_c["evidence_columns"] = kept
        cleaned.append(cleaned_c)
    return cleaned, warnings


async def run_advisor(inputs: RecommenderInputs) -> tuple[AdvisorPayload, Optional[str]]:
    """Invoke the business-concept-advisor skill via the Claude Code SDK.
    Mirrors deployment_reflection.run_reflector + qa_execute.run_executor."""
    try:
        from claude_agent_sdk import query, ClaudeAgentOptions
        from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock
    except ImportError:
        return AdvisorPayload(concepts=[]), "claude-agent-sdk is not installed"

    payload = {
        "products": inputs.products,
        "domain_catalogs": inputs.domain_catalogs,
        "osi_models": inputs.osi_models,
    }
    input_json = json.dumps(payload, default=str)[:_MAX_INPUT_JSON_CHARS]

    system_prompt = (
        f"FIRST: Load the `{ADVISOR_SKILL}` skill via the Skill tool, then follow its "
        "instructions exactly. Walk the cross-product graph snapshot in the user "
        "message and propose ranked business concept candidates with column-level "
        "evidence. Emit ONE fenced json block with `narrative` + `concepts[]`. Every "
        "concept MUST cite ≥1 `column_uri` from the input. Do not write files. Do "
        "not run shell commands. Do not answer in prose outside the json block."
    )

    user_prompt = (
        f"FIRST: Load the {ADVISOR_SKILL} skill using the Skill tool.\n\n"
        f"```json\n{input_json}\n```\n\n"
        "Emit one fenced ```json block with `narrative` (markdown) + `concepts[]` "
        "(0-15 entries). Every concept's `evidence_columns` MUST cite ≥1 column_uri "
        "from the input."
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
                from .llm_usage import extract_usage, record_usage
                record_usage(source="semantic_recommender", usage=extract_usage(message))
                if getattr(message, "is_error", False):
                    return AdvisorPayload(concepts=[]), "Advisor skill returned an error"
    except Exception as e:
        return AdvisorPayload(concepts=[]), f"Advisor skill failed: {e}"

    raw = _parse_advisor_payload("\n".join(transcript_parts))
    # Build the set of known column URIs from inputs for citation validation.
    known_uris: set[str] = set()
    for p in inputs.products:
        for d in p.get("datasets") or []:
            for c in d.get("columns") or []:
                if c.get("column_uri"):
                    known_uris.add(c["column_uri"])
    cleaned, _warnings = _validate_concepts(raw.concepts or [], known_uris)
    raw.concepts = cleaned
    return raw, None


# ── Persistence ────────────────────────────────────────────────────────────


_PERSIST_BATCH = """
CREATE (br:RecommendationBatch {
  uri: $batch_uri,
  batchId: $batch_id,
  evaluatorVersion: $evaluator_version,
  narrative: $narrative,
  productsConsidered: $products_considered,
  evaluatedAt: datetime(),
  triggeredBy: $triggered_by,
  advisorError: $advisor_error,
  conceptsJson: $concepts_json
})
RETURN br.uri AS uri, br.batchId AS batch_id
"""


_PERSIST_CONCEPT = """
MATCH (br:RecommendationBatch {uri: $batch_uri})
CREATE (bcr:BusinessConceptRecommendation {
  uri: $concept_uri,
  batchUri: $batch_uri,
  conceptName: $concept_name,
  definition: $definition,
  confidence: $confidence,
  evidenceTermsJson: $evidence_terms_json,
  evidenceColumnsJson: $evidence_columns_json,
  suggestedRelationshipsJson: $suggested_relationships_json,
  synonymsJson: $synonyms_json,
  valuesJson: $values_json,
  status: 'pending',
  createdAt: datetime()
})
CREATE (br)-[:HAS_RECOMMENDATION]->(bcr)
WITH bcr
UNWIND $evidence_column_uris AS col_uri
OPTIONAL MATCH (pc:DProdColumn {uri: col_uri})
FOREACH (_ IN CASE WHEN pc IS NULL THEN [] ELSE [1] END |
  CREATE (bcr)-[:EVIDENCED_BY]->(pc)
)
RETURN bcr.uri AS uri
"""


def persist_recommendations(
    neo4j_settings: AppSettings, payload: AdvisorPayload,
    inputs: RecommenderInputs, advisor_error: Optional[str], triggered_by: str,
) -> dict[str, Any]:
    """Persist the advisor's output as one :RecommendationBatch sidecar
    plus one :BusinessConceptRecommendation per concept. Append-only;
    every run creates a new batch."""
    batch_id = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
    batch_uri = f"semrec:batch:{batch_id}"

    persisted_concepts: list[str] = []
    with neo4j_session(
        neo4j_settings.neo4j_host, neo4j_settings.neo4j_port,
        neo4j_settings.neo4j_user, neo4j_settings.neo4j_password, neo4j_settings.neo4j_database,
    ) as ns:
        ns.run(
            _PERSIST_BATCH,
            batch_uri=batch_uri,
            batch_id=batch_id,
            evaluator_version=EVALUATOR_VERSION,
            narrative=payload.narrative or "",
            products_considered=len(inputs.products),
            triggered_by=triggered_by,
            advisor_error=advisor_error,
            concepts_json=json.dumps(payload.concepts or []),
        ).consume()

        for i, c in enumerate(payload.concepts or []):
            concept_uri = f"{batch_uri}:concept:{i:02d}"
            evidence_columns = c.get("evidence_columns") or []
            ns.run(
                _PERSIST_CONCEPT,
                batch_uri=batch_uri,
                concept_uri=concept_uri,
                concept_name=c.get("concept_name") or f"Concept {i}",
                definition=c.get("definition") or "",
                confidence=float(c.get("confidence") or 0.0),
                evidence_terms_json=json.dumps(c.get("evidence_terms") or []),
                evidence_columns_json=json.dumps(evidence_columns),
                suggested_relationships_json=json.dumps(c.get("suggested_relationships") or []),
                synonyms_json=json.dumps([str(s).strip() for s in (c.get("synonyms") or []) if str(s).strip()][:6]),
                values_json=json.dumps([
                    {
                        "name": str(v.get("name") or "").strip(),
                        "definition": str(v.get("definition") or "").strip(),
                        "value_token": str(v.get("value_token") or "").strip(),
                    }
                    for v in (c.get("values") or [])
                    if isinstance(v, dict) and v.get("name") and v.get("value_token")
                ][:8]),
                evidence_column_uris=[ev.get("column_uri") for ev in evidence_columns if ev.get("column_uri")],
            ).consume()
            persisted_concepts.append(concept_uri)

    return {
        "batch_uri": batch_uri,
        "batch_id": batch_id,
        "concepts_persisted": len(persisted_concepts),
        "concept_uris": persisted_concepts,
    }


_READ_LATEST_BATCH = """
MATCH (br:RecommendationBatch)
WITH br ORDER BY br.evaluatedAt DESC LIMIT 1
OPTIONAL MATCH (br)-[:HAS_RECOMMENDATION]->(bcr:BusinessConceptRecommendation)
WITH br, bcr ORDER BY bcr.confidence DESC, bcr.conceptName
WITH br, collect(CASE WHEN bcr IS NULL THEN NULL ELSE {
  uri: bcr.uri,
  concept_name: bcr.conceptName,
  definition: bcr.definition,
  confidence: bcr.confidence,
  status: coalesce(bcr.status, 'pending'),
  rejection_reason: bcr.rejectionReason,
  edited_at: toString(bcr.editedAt),
  evidence_terms_json: bcr.evidenceTermsJson,
  evidence_columns_json: bcr.evidenceColumnsJson,
  suggested_relationships_json: bcr.suggestedRelationshipsJson,
  synonyms_json: bcr.synonymsJson,
  values_json: bcr.valuesJson
} END) AS concepts_raw
RETURN br.uri               AS batch_uri,
       br.batchId            AS batch_id,
       br.narrative          AS narrative,
       br.productsConsidered AS products_considered,
       toString(br.evaluatedAt) AS evaluated_at,
       br.triggeredBy        AS triggered_by,
       br.advisorError       AS advisor_error,
       [c IN concepts_raw WHERE c IS NOT NULL] AS concepts
"""


def _decode_json(maybe_json: Any, default: Any) -> Any:
    if isinstance(maybe_json, (list, dict)):
        return maybe_json
    if not maybe_json:
        return default
    try:
        return json.loads(maybe_json)
    except (TypeError, ValueError):
        return default


def read_latest(neo4j_settings: AppSettings) -> Optional[dict[str, Any]]:
    """Return the most recent :RecommendationBatch with all its concepts
    expanded into a list of {uri, concept_name, definition, confidence,
    status, evidence_terms, evidence_columns, suggested_relationships}.
    None when no batch has been run yet."""
    with neo4j_session(
        neo4j_settings.neo4j_host, neo4j_settings.neo4j_port,
        neo4j_settings.neo4j_user, neo4j_settings.neo4j_password, neo4j_settings.neo4j_database,
    ) as ns:
        row = ns.run(_READ_LATEST_BATCH).single()
    if not row:
        return None
    rd = dict(row)
    expanded: list[dict[str, Any]] = []
    for c in rd.get("concepts") or []:
        c = dict(c)
        c["evidence_terms"] = _decode_json(c.pop("evidence_terms_json", None), [])
        c["evidence_columns"] = _decode_json(c.pop("evidence_columns_json", None), [])
        c["suggested_relationships"] = _decode_json(c.pop("suggested_relationships_json", None), [])
        c["synonyms"] = _decode_json(c.pop("synonyms_json", None), [])
        c["values"] = _decode_json(c.pop("values_json", None), [])
        expanded.append(c)
    return {
        "batch_uri": rd.get("batch_uri"),
        "batch_id": rd.get("batch_id"),
        "narrative": rd.get("narrative") or "",
        "products_considered": rd.get("products_considered"),
        "evaluated_at": rd.get("evaluated_at"),
        "triggered_by": rd.get("triggered_by"),
        "advisor_error": rd.get("advisor_error"),
        "concepts": expanded,
    }


_REJECT_CONCEPT = """
MATCH (bcr:BusinessConceptRecommendation {uri: $concept_uri})
SET bcr.status = 'rejected',
    bcr.rejectionReason = $reason,
    bcr.rejectionCategory = $category,
    bcr.editedAt = datetime()
WITH bcr
CREATE (pra:ProvRejectionReason {
  uri: 'prov:reject:' + $concept_uri + ':' + randomUUID(),
  rejectionCategory: $category,
  rejectionDetail: $reason,
  occurredAt: datetime()
})
CREATE (bcr)-[:HAS_REJECTION]->(pra)
RETURN bcr.uri AS uri, bcr.status AS status
"""


def reject_concept(
    neo4j_settings: AppSettings, concept_uri: str, reason: str, category: str = "other",
) -> dict[str, Any]:
    """Mark a recommendation as rejected and attach a `:ProvRejectionReason`
    (mirrors how product-request rejections are recorded). Idempotent —
    re-rejecting just updates the reason text."""
    with neo4j_session(
        neo4j_settings.neo4j_host, neo4j_settings.neo4j_port,
        neo4j_settings.neo4j_user, neo4j_settings.neo4j_password, neo4j_settings.neo4j_database,
    ) as ns:
        row = ns.run(_REJECT_CONCEPT, concept_uri=concept_uri,
                      reason=reason, category=category).single()
    if not row:
        return {"status": "not_found"}
    return dict(row)


_EDIT_CONCEPT = """
MATCH (bcr:BusinessConceptRecommendation {uri: $concept_uri})
SET bcr.conceptName = coalesce($concept_name, bcr.conceptName),
    bcr.definition = coalesce($definition, bcr.definition),
    bcr.status = CASE WHEN $concept_name IS NOT NULL OR $definition IS NOT NULL
                       THEN 'edited' ELSE bcr.status END,
    bcr.editedAt = datetime()
RETURN bcr.uri AS uri, bcr.conceptName AS concept_name,
       bcr.definition AS definition, bcr.status AS status
"""


def edit_concept(
    neo4j_settings: AppSettings, concept_uri: str,
    concept_name: Optional[str] = None, definition: Optional[str] = None,
) -> dict[str, Any]:
    """Update name + definition on a recommendation. Status flips to
    'edited' on first edit so the Steward can see what they've touched."""
    with neo4j_session(
        neo4j_settings.neo4j_host, neo4j_settings.neo4j_port,
        neo4j_settings.neo4j_user, neo4j_settings.neo4j_password, neo4j_settings.neo4j_database,
    ) as ns:
        row = ns.run(_EDIT_CONCEPT, concept_uri=concept_uri,
                      concept_name=concept_name, definition=definition).single()
    if not row:
        return {"status": "not_found"}
    return dict(row)


def to_export_yaml(batch: dict[str, Any]) -> str:
    """Render the latest batch as a YAML document the Steward can download
    and feed into a downstream concept-curation workflow. Excludes rejected
    recommendations by default."""
    concepts_out = []
    for c in batch.get("concepts") or []:
        if c.get("status") == "rejected":
            continue
        concepts_out.append({
            "concept_name": c.get("concept_name"),
            "definition": c.get("definition") or "",
            "confidence": c.get("confidence"),
            "evidence_terms": c.get("evidence_terms") or [],
            "evidence_columns": [
                {
                    "column_uri": ev.get("column_uri"),
                    "product_name": ev.get("product_name"),
                    "score": ev.get("score"),
                    "why": ev.get("why"),
                }
                for ev in (c.get("evidence_columns") or [])
                if ev.get("column_uri")
            ],
            "suggested_relationships": c.get("suggested_relationships") or [],
            "synonyms": c.get("synonyms") or [],
        })
    doc = {
        "schema": "business-concept-recommendations.v0.1.0",
        "batch_id": batch.get("batch_id"),
        "evaluated_at": batch.get("evaluated_at"),
        "products_considered": batch.get("products_considered"),
        "concepts": concepts_out,
    }
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
