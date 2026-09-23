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
    return await run_agent("silver-sttm", user_input, context=context, session=session)


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
