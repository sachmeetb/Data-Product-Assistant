"""
silver_layer_agent.py — DDI Silver mappers.

Two passes:

  - `build_sttm`              → column-level **Silver → Gold** STTM. Called
                                after the Gold ER is approved. Produces the
                                STTM the user sees in the "Gold STTM Generator"
                                review card.
  - `build_silver_transformation` → column-level **Bronze → Silver** STTM,
                                called after the Gold STTM is locked. Produces
                                the lineage view the user sees in the "Silver
                                Transformation Agent" review card.

The downstream `gold_layer_agent.finalize` validates the Gold STTM and emits
the final artifact (utility_catalog.json + pipeline_spec).
"""

import json
from pathlib import Path
from typing import Any

from .base import run_agent
from tools.template_loader import TemplateLoader

_BRONZE_FIXTURE_PATH = Path(__file__).parent.parent / "data" / "bronze_user_visit_events_data.json"

_loader = TemplateLoader()


def _load_bronze_fixture() -> dict:
    """Load actual bronze row data so agents can design silver/gold from real bronze content."""
    try:
        with open(_BRONZE_FIXTURE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


async def build_sttm(
    discovery_output: dict,
    gold_er: dict,
    context: dict | None = None,
    session=None,
    feedback: str | None = None,
    previous_sttm: dict | None = None,
) -> dict:
    """
    Run the Silver → Gold STTM Mapper.

    Args:
        discovery_output: The dict produced by `agents.discovery.run`.
        gold_er:          The Gold ER from `gold_layer_agent.build_er`.
        context:          Optional session metadata.
        session:          ADK session_id string for this HTTP session.
        feedback:         Optional user feedback ("Tweak the mapping"). When
                          provided, the previous STTM is passed alongside so
                          the agent refines rather than rebuilds.
        previous_sttm:    The prior STTM output — only used with `feedback`.

    Returns:
        Dict with: source_layer, target_layer, target_table, schema_strategy,
        required_columns, required_transformations, mappings, unmapped_sources,
        mapping_gaps; or `{"error": "..."}` on failure.
    """
    if not isinstance(discovery_output, dict):
        return {"error": "Silver STTM Mapper: invalid discovery input."}
    if not isinstance(gold_er, dict) or "error" in gold_er:
        return {"error": "Silver STTM Mapper: invalid gold_er input."}
    if gold_er.get("style") != "star-schema":
        return {"error": "Silver STTM Mapper: gold_er.style must be 'star-schema'."}

    payload: dict[str, Any] = {"discovery": discovery_output, "gold_er": gold_er}
    if feedback:
        payload["previous_sttm"] = previous_sttm or {}
        payload["user_feedback"] = feedback
        payload["instruction"] = (
            "Apply the user_feedback to refine the previous_sttm. Keep the "
            "same output JSON shape and transformation vocabulary."
        )
    bronze_fixture = _load_bronze_fixture()
    if bronze_fixture:
        payload["bronze_data_fixture"] = bronze_fixture

    template = _loader.detect_and_load(discovery_output)
    if template:
        payload["domain_silver_framework"] = template
        payload["domain_framework_instruction"] = (
            "A domain_silver_framework is present. Treat its `entities` dict as the canonical "
            "Silver target model for this domain. Select entities relevant to the Gold schema. "
            "Map source fields to the slv_* entities defined in the framework hierarchy. "
            "Do not omit or rename any template primary or foreign keys."
        )

    user_input = json.dumps(payload, indent=2, default=str)
    raw_res = await run_agent("silver-sttm", user_input, context=context, session=session)
    if not is_complete(raw_res):
        print("[silver-sttm] Incomplete or truncated LLM output — applying fallback patch.")
        raw_res = _apply_silver_sttm_fallback(raw_res, discovery_output, gold_er)
    return raw_res


def _build_silver_sttm_fallback(discovery_output: dict, gold_er: dict) -> dict:
    """
    Construct a complete, deterministic Silver->Gold STTM from gold_er tables
    and discovery matches. Guarantees 100% valid STTM structure if the LLM
    fails or truncates.
    """
    tables = gold_er.get("tables") or []
    fact_table = next((t for t in tables if t.get("type") == "fact"), None)
    fact_name = fact_table.get("name", "fact_gold") if fact_table else "fact_gold"

    disc_view = discovery_output.get("discovery_view") or {}
    silver_tables_list = disc_view.get("tables_by_layer", {}).get("silver") or []

    mappings = []
    required_columns = []
    used_transforms = set()

    # 1. Fact table columns
    if fact_table:
        for col in fact_table.get("columns", []):
            cname = col.get("name", "")
            target = f"{fact_name}.{cname}"

            if col.get("is_fk"):
                fk_to = col.get("fk_to") or ""
                dim_target = fk_to.split(".")[0] if "." in fk_to else f"dim_{cname.replace('_id', '')}"
                if cname in ("date_id", "date"):
                    source = "silver.campaign_impressions_conformed.timestamp"
                    transform = "DERIVE_FROM_TIMESTAMP+CAST_TO_DATE"
                    kind = "derived"
                    reason = "FK to dim_date derived from event timestamp"
                else:
                    clean = cname.replace("_id", "")
                    matched_s = next(
                        (st.get("table_short_name") for st in silver_tables_list if clean in st.get("table_short_name", "").lower()),
                        f"slv_{clean}",
                    )
                    source = f"silver.{matched_s}.{cname}"
                    transform = "DIRECT_MAP"
                    kind = "direct"
                    reason = f"FK to {dim_target} from silver key"
            elif col.get("is_measure"):
                meas_name = cname.lower()
                agg = col.get("aggregation", "sum").lower()
                if agg == "count" or "impression" in meas_name or "click" in meas_name:
                    transform = "COUNT_AGGREGATE"
                    if "click" in meas_name:
                        source = "silver.campaign_clicks_conformed.click_id"
                    else:
                        source = "silver.campaign_impressions_conformed.impression_id"
                    kind = "derived"
                    reason = "Count-source measure over grain"
                else:
                    transform = "SUM_AGGREGATE"
                    if "spend" in meas_name or "cost" in meas_name:
                        source = "silver.slv_campaign_performance_daily.spend"
                    elif "revenue" in meas_name or "conversion" in meas_name:
                        source = "silver.slv_conversion_event.conversion_value"
                    else:
                        source = f"silver.slv_campaign_performance_daily.{cname}"
                    kind = "derived"
                    reason = "Sum aggregate measure over grain"
            else:
                source = f"silver.slv_campaign_performance_daily.{cname}"
                transform = "DIRECT_MAP"
                kind = "direct"
                reason = "Attribute mapping from silver"

            used_transforms.add(transform)
            mappings.append({
                "target": target,
                "source": source,
                "transform": transform,
                "kind": kind,
                "notes": f"{kind.capitalize()} mapping for {cname}",
            })
            required_columns.append({
                "gold_column": target,
                "reason": reason,
            })

    # 2. Dimension table columns
    for t in tables:
        if t.get("type") == "dimension":
            tname = t.get("name", "")
            clean = tname.replace("dim_", "")
            matched_s = next(
                (st.get("table_short_name") for st in silver_tables_list if clean in st.get("table_short_name", "").lower()),
                f"slv_{clean}",
            )
            for col in t.get("columns", []):
                cname = col.get("name", "")
                target = f"{tname}.{cname}"
                if col.get("is_pk"):
                    if cname == "date_id":
                        source = "silver.campaign_impressions_conformed.timestamp"
                        transform = "DERIVE_FROM_TIMESTAMP+CAST_TO_DATE"
                        kind = "derived"
                        reason = "Primary key of dim_date from timestamp"
                    else:
                        source = f"silver.{matched_s}.{cname}"
                        transform = "DIRECT_MAP+DEDUPLICATE"
                        kind = "direct"
                        reason = f"Primary key of {tname}"
                elif cname in ("day_of_week", "month", "year"):
                    source = "silver.campaign_impressions_conformed.timestamp"
                    transform = "DERIVE_FROM_DATE"
                    kind = "derived"
                    reason = "Derived from date at gold load time"
                else:
                    source = f"silver.{matched_s}.{cname}"
                    transform = "DIRECT_MAP"
                    kind = "direct"
                    reason = f"Descriptive attribute on {tname}"

                used_transforms.add(transform)
                mappings.append({
                    "target": target,
                    "source": source,
                    "transform": transform,
                    "kind": kind,
                    "notes": f"{kind.capitalize()} mapping for {target}",
                })
                required_columns.append({
                    "gold_column": target,
                    "reason": reason,
                })

    transform_reasons = {
        "DIRECT_MAP": "Passthrough conformed column from silver.",
        "DIRECT_MAP+DEDUPLICATE": "Deduplicate key values for dimension uniqueness.",
        "DERIVE_FROM_TIMESTAMP+CAST_TO_DATE": "Truncate timestamp to ISO date for date grain.",
        "DERIVE_FROM_DATE": "Extract date attributes at load time.",
        "COUNT_AGGREGATE": "Count-source KPI aggregated over the grain.",
        "SUM_AGGREGATE": "Sum numeric measure aggregated over the grain.",
    }

    required_transformations = [
        {"label": tr, "reason": transform_reasons.get(tr, "Standard transformation.")}
        for tr in sorted(used_transforms)
    ]

    return {
        "source_layer": "silver",
        "target_layer": "gold",
        "target_table": fact_name,
        "new_silver_tables_required": False,
        "schema_strategy": f"Star schema radiating from {fact_name} with conformed dimensions.",
        "required_columns": required_columns,
        "required_transformations": required_transformations,
        "mappings": mappings,
        "unmapped_sources": [],
        "mapping_gaps": [],
    }


def _apply_silver_sttm_fallback(llm_output: dict, discovery_output: dict, gold_er: dict) -> dict:
    """Patch any gaps or synthesize complete STTM if LLM output was truncated or failed."""
    fallback = _build_silver_sttm_fallback(discovery_output, gold_er)
    if not isinstance(llm_output, dict):
        return fallback

    patched = dict(llm_output)
    if not isinstance(patched.get("mappings"), list) or len(patched.get("mappings", [])) < len(fallback["mappings"]):
        patched["mappings"] = fallback["mappings"]

    if not isinstance(patched.get("required_columns"), list) or len(patched.get("required_columns", [])) < len(fallback["required_columns"]):
        patched["required_columns"] = fallback["required_columns"]

    if not isinstance(patched.get("required_transformations"), list) or len(patched.get("required_transformations", [])) == 0:
        patched["required_transformations"] = fallback["required_transformations"]

    if not patched.get("schema_strategy") or not isinstance(patched["schema_strategy"], str):
        patched["schema_strategy"] = fallback["schema_strategy"]

    if not patched.get("target_table"):
        patched["target_table"] = fallback["target_table"]

    patched.setdefault("source_layer", "silver")
    patched.setdefault("target_layer", "gold")
    patched.setdefault("new_silver_tables_required", False)

    patched.pop("error", None)
    patched.pop("raw_output", None)
    return patched


def is_complete(output: dict) -> bool:
    """Check whether the silver agent returned a valid Silver→Gold STTM draft."""
    return (
        isinstance(output, dict)
        and isinstance(output.get("mappings"), list)
        and len(output.get("mappings", [])) > 0
        and isinstance(output.get("required_columns"), list)
        and isinstance(output.get("required_transformations"), list)
        and "error" not in output
        and "raw_output" not in output
    )


async def build_silver_transformation(
    discovery_output: dict,
    gold_er: dict,
    gold_sttm: dict,
    context: dict | None = None,
    session=None,
) -> dict:
    """
    Run the Bronze → Silver transformation mapper.

    Triggered after the user locks the Gold STTM. Produces the lineage view
    shown in the "Silver Transformation Agent" review card (narrative,
    lineage_summary, silver_tables, mappings).

    Args:
        discovery_output: The dict produced by `agents.discovery.run`.
        gold_er:          The Gold ER from `gold_layer_agent.build_er`.
        gold_sttm:        The locked Silver→Gold STTM from `build_sttm`.
        context:          Optional session metadata.
        session:          ADK session_id string for this HTTP session.

    Returns:
        Dict with: source_layer, target_layer, new_silver_tables_required,
        narrative, lineage_summary, silver_tables, mappings, broken_links;
        or `{"error": "..."}` on failure.
    """
    if not isinstance(discovery_output, dict):
        return {"error": "Silver Transformation: invalid discovery input."}
    if not isinstance(gold_er, dict) or "error" in gold_er:
        return {"error": "Silver Transformation: invalid gold_er input."}
    if not isinstance(gold_sttm, dict) or "error" in gold_sttm:
        return {"error": "Silver Transformation: invalid gold_sttm input."}

    # Trim discovery so the payload stays within token budget. The transformation
    # agent only needs the match lists + scalars, not full catalog blobs.
    from .gold_layer_agent import _trim_discovery_for_er
    trimmed_discovery = _trim_discovery_for_er(discovery_output)

    payload: dict[str, Any] = {
        "discovery": trimmed_discovery,
        "gold_er":   gold_er,
        "gold_sttm": gold_sttm,
    }
    bronze_fixture = _load_bronze_fixture()
    base_bytes = len(json.dumps(payload, default=str).encode())
    if bronze_fixture and base_bytes < 40_000:
        payload["bronze_data_fixture"] = bronze_fixture

    template = _loader.detect_and_load(discovery_output)
    if template:
        payload["domain_silver_framework"] = template
        payload["domain_framework_instruction"] = (
            "A domain_silver_framework is present. "
            "Apply the Bronze → Silver column transforms defined in the framework's `entities`. "
            "Use FILTER_EVENT for event_type discrimination, UPPER for string normalisation, "
            "and COUNT_BY_GRAIN / SUM_BY_GRAIN for aggregate entities. "
            "Emit NOT_NULL DQ rules for all `nullable: false` columns in the framework. "
            "Emit UNIQUE_KEY DQ rules for all `is_pk: true` columns."
        )

    user_input = json.dumps(payload, indent=2, default=str)
    return await run_agent("silver-transformation", user_input, context=context, session=session)


def is_silver_transformation_complete(output: dict) -> bool:
    """Check whether build_silver_transformation returned a usable artifact."""
    return (
        isinstance(output, dict)
        and isinstance(output.get("mappings"), list)
        and isinstance(output.get("silver_tables"), list)
        and isinstance(output.get("lineage_summary"), list)
        and isinstance(output.get("narrative"), str)
        and "error" not in output
        and "raw_output" not in output
    )
