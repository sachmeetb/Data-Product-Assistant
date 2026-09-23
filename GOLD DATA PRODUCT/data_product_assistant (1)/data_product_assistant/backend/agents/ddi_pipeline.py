"""
ddi_pipeline.py — DDI orchestration: Discovery output → Gold ER → Silver STTM → Gold Final.

Public entrypoints:
  - validate_discovery_payload(payload): light shape check used by the server when
    a user uploads a JSON via the DDI starting-point card.
  - run(discovery_output, ctx, session): async; runs the three-pass DDI flow
    and returns the assembled blueprint dict (or {"status":"failed",...}).
  - extract_pipeline_spec(blueprint): pulls the `pipeline_spec` block out of the
    blueprint for direct hand-off to `pipeline_generator.run(spec)`.

Consumes the Discovery Agent's catalog-match JSON (gold_matches/silver_matches/
bronze_matches) and produces a utility_catalog.json-shaped data catalog plus a
pipeline_spec for direct hand-off to the DPB pipeline-generator.

NOTE: utility_catalog.json is the read-only POC seed catalog. The DDI flow never
writes to it — all DDI output is persisted to the blueprint artifact only.
"""

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import gold_layer_agent, silver_layer_agent


_BACKEND_ROOT = Path(__file__).parent.parent
_DATA_DIR = _BACKEND_ROOT / "data"
_BLUEPRINT_PATH = _DATA_DIR / "ddi_blueprint.json"


def validate_discovery_payload(payload: Any) -> Optional[str]:
    """
    Light validation of a DDI input JSON. Returns None on success, or an error
    message string on failure.

    Per project decision, validation is intentionally permissive — we only
    require the payload to be a JSON object with at least one of the
    `gold_matches`, `silver_matches`, `bronze_matches` keys (any list).
    """
    if not isinstance(payload, dict):
        return "DDI input must be a JSON object."

    has_any = False
    for key in ("gold_matches", "silver_matches", "bronze_matches"):
        v = payload.get(key)
        if isinstance(v, list):
            has_any = True
            break
    if not has_any:
        return (
            "DDI input does not look like a discovery output — expected one of "
            "`gold_matches`, `silver_matches`, or `bronze_matches` (list)."
        )
    return None


async def run(
    discovery_output: dict,
    context: dict | None = None,
    session=None,
    verbose: bool = False,
) -> dict:
    """
    Run gold.build_er → silver.build_sttm → gold.finalize.

    Args:
        discovery_output: The discovery agent's final output JSON.
        context:          Optional session metadata (forwarded to each agent).
        session:          Optional MAF AgentSession (forwarded to each agent).
        verbose:          When True, print step-by-step timing to stdout.

    Returns:
        On success — a blueprint dict with status='completed' and the keys:
          blueprint_id, generated_at, pipeline, gold_er, silver_sttm,
          gold_final, run_metadata, status='completed'.
        On failure — `{"status": "failed", "error": "..."}`.
    """
    blueprint_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc)
    session_ctx: dict = {"session_id": blueprint_id, **(context or {})}

    def _log(msg: str) -> None:
        if verbose:
            print(msg)

    err = validate_discovery_payload(discovery_output)
    if err:
        return {"status": "failed", "error": err}

    _log(f"[DDI] Blueprint ID  : {blueprint_id}")
    _log(f"[DDI] Use case      : {discovery_output.get('discovery_view', {}).get('use_case', '(unknown)')}")

    # ── Step 1: Gold ER (pass 1) ──────────────────────────────────────────────
    _log("\n[DDI] [step 1/3] gold-er ...")
    t0 = time.perf_counter()
    gold_er = await gold_layer_agent.build_er(
        discovery_output, context=session_ctx, session=session
    )
    er_dur = time.perf_counter() - t0

    if "error" in gold_er:
        return {"status": "failed", "error": f"gold-er: {gold_er['error']}"}
    if not gold_layer_agent.is_er_complete(gold_er):
        return {
            "status": "failed",
            "error": f"gold-er: incomplete output. Keys: {sorted(gold_er.keys())}",
        }
    _log(f"[DDI] [step 1/3] gold-er ... done in {er_dur:.1f}s "
         f"({len(gold_er.get('tables', []))} tables)")

    # ── Step 2: Silver STTM ───────────────────────────────────────────────────
    _log("\n[DDI] [step 2/3] silver-sttm ...")
    t0 = time.perf_counter()
    silver_sttm = await silver_layer_agent.build_sttm(
        discovery_output, gold_er, context=session_ctx, session=session
    )
    sttm_dur = time.perf_counter() - t0

    if "error" in silver_sttm:
        return {"status": "failed", "error": f"silver-sttm: {silver_sttm['error']}"}
    if not silver_layer_agent.is_complete(silver_sttm):
        return {
            "status": "failed",
            "error": f"silver-sttm: incomplete output. Keys: {sorted(silver_sttm.keys())}",
        }
    _log(f"[DDI] [step 2/3] silver-sttm ... done in {sttm_dur:.1f}s "
         f"({len(silver_sttm.get('mappings', []))} mappings)")

    # ── Step 3: Gold Final (pass 2) ───────────────────────────────────────────
    _log("\n[DDI] [step 3/3] gold-final ...")
    t0 = time.perf_counter()
    gold_final = await gold_layer_agent.finalize(
        gold_er, silver_sttm, context=session_ctx, session=session
    )
    fin_dur = time.perf_counter() - t0

    if "error" in gold_final:
        return {"status": "failed", "error": f"gold-final: {gold_final['error']}"}
    if not gold_layer_agent.is_final_complete(gold_final):
        return {
            "status": "failed",
            "error": (
                f"gold-final: incomplete output. Keys: {sorted(gold_final.keys())}"
            ),
        }
    validation_status = gold_final.get("validation", {}).get("status", "unknown")
    _log(f"[DDI] [step 3/3] gold-final ... done in {fin_dur:.1f}s "
         f"(validation: {validation_status})")

    finished_at = datetime.now(timezone.utc)
    blueprint: dict[str, Any] = {
        "blueprint_id":     blueprint_id,
        "generated_at":     finished_at.isoformat(),
        "pipeline":         "gold-er -> silver-sttm -> gold-final",
        "discovery_input":  discovery_output,
        "gold_er":          gold_er,
        "silver_sttm":      silver_sttm,
        "gold_final":       gold_final,
        "run_metadata": {
            "agents_run":       ["gold-er", "silver-sttm", "gold-final"],
            "started_at":       started_at.isoformat(),
            "finished_at":      finished_at.isoformat(),
            "duration_seconds": round((finished_at - started_at).total_seconds(), 3),
            "mapping_count":    len(silver_sttm.get("mappings", [])),
            "validation":       validation_status,
        },
        "status": "completed",
    }

    return blueprint


def write_blueprint(blueprint: dict, target_path: Path | str | None = None) -> Path:
    """Persist the full blueprint (debug/audit artifact)."""
    target = Path(target_path) if target_path else _BLUEPRINT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        json.dump(blueprint, f, indent=2, ensure_ascii=False, default=str)
    return target


def extract_pipeline_spec(blueprint: dict) -> dict:
    """
    Pull the `pipeline_spec` block out of the DDI blueprint so it can be passed
    directly to `pipeline_generator.run(spec=...)`.

    Falls back to a Python-built spec if the gold-final agent did not emit a
    `pipeline_spec` block (defensive — keeps the chain working if the LLM
    skips that section).
    """
    gold_final = blueprint.get("gold_final") or {}
    spec = gold_final.get("pipeline_spec")
    if isinstance(spec, dict) and (spec.get("target_tables") or spec.get("gold_schema")):
        return spec

    # ── Fallback assembly ────────────────────────────────────────────────────
    tables = gold_final.get("tables") or []
    sttm_mappings = gold_final.get("sttm") or []
    domain = gold_final.get("domain") or "general"

    target_tables = [
        {
            "name": t.get("name", ""),
            "layer": "gold",
            "columns": t.get("columns", []),
            "description": t.get("grain", "") if t.get("type") == "fact" else "",
        }
        for t in tables
        if t.get("name")
    ]

    return {
        "pipeline_type":      "sql",
        "use_case_name":      gold_final.get("use_case", ""),
        "domain":             domain,
        "catalog":            "bq_project",
        "target_environment": "dev",
        "gold_schema":        {"tables": tables},
        "sttm":               {"mappings": [{**m, "layer": "gold"} for m in sttm_mappings]},
        "target_tables":      target_tables,
        "source_tables":      gold_final.get("source_tables", []),
    }
