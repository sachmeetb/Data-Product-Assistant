"""
pipeline.py — BFSI Silver Agent Pipeline Orchestrator.

End-to-end flow:
  1. Bank Profile Agent       — collect bank context
  2. Requirement Understanding — natural language → StructuredRequirement
  3. Domain Scoping Agent     — map to BIAN + Silver entities
  4. Silver Product Engine    — detailed column-level table specs
  5. Spec Generator Agent     — BigQuery DDL + formal specification
  6. Validator Agent          — standards compliance check
  7. BigQuery Publisher       — execute DDL in GCP (if valid)

Usage (CLI):
  cd BFSI-Silver-Agent
  python pipeline.py                     # interactive mode
  python pipeline.py samples/sample.json # run from JSON spec file
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from agents import (
    bank_profile_agent,
    requirement_understanding_agent,
    domain_scoping_agent,
    silver_product_engine,
    spec_generator_agent,
    validator_agent,
)
from agents.base import create_session
from tools.bigquery_tool import BigQueryPublisher

MAX_CLARIFICATION_TURNS = 3
MAX_SPEC_ITERATIONS = 3
MAX_AGENT_RETRIES = 3


def _log(msg: str) -> None:
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode("ascii"), flush=True)


# ── Retry helper ──────────────────────────────────────────────────────────────

async def _retry_agent(
    label: str,
    run_fn,
    is_ok_fn,
    diagnose_fn,
) -> dict:
    """
    Run an agent step with automatic retries on failure.

    Args:
        label:       Human-readable name for log messages.
        run_fn:      async callable(feedback: str | None) -> dict
                     Runs the agent, optionally with correction feedback.
        is_ok_fn:    callable(output: dict) -> bool
                     Returns True when the agent output is complete/valid.
        diagnose_fn: callable(output: dict) -> str
                     Returns a diagnostic string to feed back on failure.

    Returns:
        The last output dict (caller checks is_ok_fn to determine success).
    """
    feedback: str | None = None
    output: dict = {}

    for attempt in range(1, MAX_AGENT_RETRIES + 1):
        suffix = f"(attempt {attempt}/{MAX_AGENT_RETRIES})"
        _log(f"[{label}] Running {suffix}...")
        output = await run_fn(feedback)

        if is_ok_fn(output):
            _log(f"[{label}] ✓ Complete {suffix}")
            return output

        if "error" in output:
            _log(f"[{label}] ✗ Error {suffix}: {output['error'][:200]}")
        else:
            _log(f"[{label}] ✗ Output incomplete {suffix}")

        if attempt < MAX_AGENT_RETRIES:
            feedback = diagnose_fn(output)
            _log(f"[{label}]   Retrying with feedback: {feedback[:200]}")

    _log(f"[{label}] ✗ All {MAX_AGENT_RETRIES} attempts exhausted.")
    return output


# ── Step helpers ──────────────────────────────────────────────────────────────

async def _run_bank_profile(user_input: str, session_id: str) -> dict:
    _log("\n[Pipeline] Step 1 — Bank Profile Agent...")
    output = await bank_profile_agent.run(user_input, session_id=session_id)

    turns = 0
    while not bank_profile_agent.is_complete(output) and turns < MAX_CLARIFICATION_TURNS:
        if "raw_output" in output:
            _log(f"\n[Bank Profile] {output['raw_output']}")
        elif "error" in output:
            _log(f"[Bank Profile] Error: {output['error']}")
            break
        else:
            missing = bank_profile_agent.get_missing_fields(output)
            _log(f"[Bank Profile] Missing fields: {missing}")

        user_answer = input("> ").strip()
        if not user_answer:
            break
        output = await bank_profile_agent.run(user_answer, session_id=session_id)
        turns += 1

    if not bank_profile_agent.is_complete(output):
        _log("[Bank Profile] ⚠ Profile incomplete — continuing with partial data.")
    else:
        _log(f"[Bank Profile] ✓ Profile complete: {output.get('bank_name')} ({output.get('bank_code')})")

    return output


async def _run_requirement_understanding(
    user_input: str,
    bank_profile: dict,
    session_id: str,
) -> dict:
    _log("\n[Pipeline] Step 2 — Requirement Understanding Agent...")

    context = {
        "bank_profile": bank_profile,
        "clarification_pass": 0,
        "original_input": user_input,
    }
    output = await requirement_understanding_agent.run(
        user_input, context=context, session_id=session_id
    )

    turns = 0
    while not requirement_understanding_agent.is_handoff_ready(output) and turns < MAX_CLARIFICATION_TURNS:
        if "raw_output" in output:
            _log(f"\n[Requirement] {output['raw_output']}")
        elif "error" in output:
            _log(f"[Requirement] Error: {output['error']}")
            break

        user_answer = input("> ").strip()
        if not user_answer:
            break

        context = {
            "bank_profile": bank_profile,
            "clarification_pass": turns + 1,
            "original_input": user_input,
            "prior_output": output,
        }
        output = await requirement_understanding_agent.run(
            user_answer, context=context, session_id=session_id
        )
        turns += 1

    if requirement_understanding_agent.is_handoff_ready(output):
        _log(f"[Requirement] ✓ Understood: '{output.get('use_case_name')}' in domain '{output.get('banking_domain')}'")
    else:
        _log("[Requirement] ⚠ Requirement incomplete — continuing.")

    return output


async def _run_domain_scoping(
    structured_req: dict,
    bank_profile: dict,
    session_id: str,
) -> dict:
    _log("\n[Pipeline] Step 3 — Domain Scoping Agent...")

    def _diagnose(output: dict) -> str:
        issues = []
        tables = output.get("required_tables", [])
        print("Domain_scoping tables:", tables)
        if not isinstance(tables, list) or len(tables) == 0:
            issues.append(
                "'required_tables' is missing or empty. "
                "Return a list of objects each with table_name and selected_common_blocks."
            )
        else:
            for t in tables:
                blocks = t.get("selected_common_blocks", [])
                if not isinstance(blocks, list) or "technical-metadata" not in blocks:
                    issues.append(
                        f"Table '{t.get('table_name', '?')}' is missing 'technical-metadata' "
                        "in selected_common_blocks. It is mandatory for every Silver table."
                    )
        return " | ".join(issues) if issues else "Output shape validation failed."

    async def _run(feedback: str | None) -> dict:
        extra = {"correction_required": feedback} if feedback else None
        return await domain_scoping_agent.run(
            structured_req, bank_profile, session_id=session_id, extra_context=extra
        )

    output = await _retry_agent(
        label="Domain Scoping",
        run_fn=_run,
        is_ok_fn=domain_scoping_agent.is_complete,
        diagnose_fn=_diagnose,
    )

    if domain_scoping_agent.is_complete(output):
        tables = domain_scoping_agent.get_recommended_tables(output)
        regs   = domain_scoping_agent.get_regulatory_overlays(output)
        _log(f"[Domain Scope] ✓ Tables: {tables}")
        if regs:
            _log(f"[Domain Scope]   Regulatory overlays: {list(regs.keys())}")
    else:
        _log("[Domain Scope] ✗ Failed after all retries.")

    return output


async def _run_silver_product_engine(
    domain_scope: dict,
    bank_profile: dict,
    structured_req: dict,
    session_id: str,
) -> dict:
    _log("\n[Pipeline] Step 4 — Silver Product Engine...")

    def _diagnose(output: dict) -> str:
        issues = []
        tables = output.get("tables", [])
        if not isinstance(tables, list) or len(tables) == 0:
            issues.append(
                "'tables' is missing or empty. Return a list of table objects "
                "each with table_name and columns."
            )
        else:
            for t in tables:
                cols     = t.get("columns", [])
                col_names = [c.get("name", "") for c in cols]
                if not cols:
                    issues.append(
                        f"Table '{t.get('table_name', '?')}' has no columns. "
                        "Expand every selected_common_block into its column list."
                    )
                elif "surrogate_key" not in col_names:
                    issues.append(
                        f"Table '{t.get('table_name', '?')}' is missing 'surrogate_key'. "
                        "The technical-metadata block must be expanded first, "
                        "surrogate_key is always the first column."
                    )
        return " | ".join(issues) if issues else "Output shape validation failed."

    async def _run(feedback: str | None) -> dict:
        extra = {"correction_required": feedback} if feedback else None
        return await silver_product_engine.run(
            domain_scope, bank_profile, structured_req,
            session_id=session_id, extra_context=extra
        )

    output = await _retry_agent(
        label="Silver Product Engine",
        run_fn=_run,
        is_ok_fn=silver_product_engine.is_complete,
        diagnose_fn=_diagnose,
    )

    if silver_product_engine.is_complete(output):
        tables = silver_product_engine.get_table_names(output)
        _log(f"[Product Engine] ✓ Tables: {tables}")
        _log(f"[Product Engine]   Blocks: {silver_product_engine.get_reuse_summary(output)}")
    else:
        _log("[Product Engine] ✗ Failed after all retries.")

    return output


async def _run_spec_generator_with_validation(
    product_plan: dict,
    bank_profile: dict,
    domain_scope: dict,
    session_id: str,
) -> dict:
    """Generate spec + DDL, validate, retry up to MAX_SPEC_ITERATIONS on failure."""
    spec_out: dict = {}
    validation_out: dict = {}
    feedback: str | None = None

    for iteration in range(1, MAX_SPEC_ITERATIONS + 1):
        _log(f"\n[Pipeline] Step 5 (iter {iteration}/{MAX_SPEC_ITERATIONS}) — Spec Generator...")
        spec_out = await spec_generator_agent.run(
            product_plan, bank_profile,
            session_id=session_id,
            prior_spec=spec_out if iteration > 1 else None,
            feedback=feedback,
        )

        if not spec_generator_agent.is_complete(spec_out):
            _log(f"[Spec Generator] ✗ Failed: {spec_out.get('error', spec_out.get('raw_output', ''))[:200]}")
            break

        ddl = spec_generator_agent.get_ddl(spec_out)
        spec = spec_generator_agent.get_specification(spec_out)
        _log(f"[Spec Generator] ✓ DDL generated ({len(ddl)} chars)")

        _log(f"\n[Pipeline] Step 6 (iter {iteration}) — Validator Agent...")
        validation_out = await validator_agent.run(
            ddl, spec, bank_profile, domain_scope, session_id=session_id
        )

        if validator_agent.is_passing(validation_out) or validation_out.get("validation_status") in ("PASSED", "PASSED_WITH_WARNINGS") or len(validator_agent.get_errors(validation_out)) == 0:
            _log(f"[Validator] ✓ {validation_out.get('validation_status', 'PASSED')} — ready to publish")
            break

        errors = validator_agent.get_errors(validation_out)
        warnings = validator_agent.get_warnings(validation_out)
        _log(f"[Validator] ✗ {len(errors)} error(s), {len(warnings)} warning(s)")
        for e in errors[:5]:
            _log(f"  ERROR: {e}")

        if iteration == MAX_SPEC_ITERATIONS:
            _log("[Validator] Max iterations reached — publishing anyway with warnings.")
            break

        feedback = f"Fix these validation errors: {'; '.join(errors[:5])}"

    return {"spec": spec_out, "validation": validation_out}


async def _publish(ddl_script: str, spec: dict) -> dict:
    _log("\n[Pipeline] Step 7 — BigQuery Publisher...")
    publisher = BigQueryPublisher()

    if not publisher.can_execute():
        _log(f"[Publisher] Mode={publisher.mode} — dry run (no GCP credentials configured)")

    report = publisher.publish(ddl_script)
    _log(f"[Publisher] Status: {report.get('publish_status')}")
    _log(f"[Publisher] {report.get('summary', '')}")

    if report.get("published_tables"):
        _log(f"[Publisher] Tables: {report['published_tables']}")

    return report


# ── Main pipeline ─────────────────────────────────────────────────────────────

async def run_pipeline(
    bank_input: str,
    requirement_input: str,
    verbose: bool = True,
) -> dict:
    """
    Run the full BFSI Silver Agent pipeline end-to-end.

    Retry behaviour per step:
      Step 1 (Bank Profile)          — up to MAX_CLARIFICATION_TURNS user turns
      Step 2 (Requirement)           — up to MAX_CLARIFICATION_TURNS user turns
      Step 3 (Domain Scoping)        — up to MAX_AGENT_RETRIES auto-retries with feedback
      Step 4 (Silver Product Engine) — up to MAX_AGENT_RETRIES auto-retries with feedback
      Step 5+6 (Spec + Validator)    — up to MAX_SPEC_ITERATIONS coupled retries

    Returns:
        Consolidated result dict with all step outputs and final status.
    """
    thread_id = str(uuid.uuid4())
    session_id = await create_session()

    results: dict = {
        "thread_id": thread_id,
        "session_id": session_id,
        "status": "in_progress",
        "steps": {},
        "retry_config": {
            "max_clarification_turns": MAX_CLARIFICATION_TURNS,
            "max_agent_retries": MAX_AGENT_RETRIES,
            "max_spec_iterations": MAX_SPEC_ITERATIONS,
        },
    }

    _log(f"\n[Pipeline] ══ BFSI Silver Agent Pipeline ══")
    _log(f"[Pipeline] Thread: {thread_id}")
    _log(
        f"[Pipeline] Retry config — clarification turns: {MAX_CLARIFICATION_TURNS}, "
        f"agent retries: {MAX_AGENT_RETRIES}, spec iterations: {MAX_SPEC_ITERATIONS}"
    )

    try:
        # ── Step 1 ────────────────────────────────────────────────────────────
        bank_profile = await _run_bank_profile(bank_input, session_id)
        results["steps"]["bank_profile"] = bank_profile

        # ── Step 2 ────────────────────────────────────────────────────────────
        structured_req = await _run_requirement_understanding(
            requirement_input, bank_profile, session_id
        )
        results["steps"]["requirement"] = structured_req

        if "error" in structured_req and not requirement_understanding_agent.is_handoff_ready(structured_req):
            results["status"] = "failed"
            results["error"] = f"Requirement Understanding failed: {structured_req.get('error', '')}"
            return results

        # ── Step 3 — Domain Scoping (retry handled by _retry_agent inside) ────
        domain_scope = await _run_domain_scoping(structured_req, bank_profile, session_id)
        results["steps"]["domain_scope"] = domain_scope

        if not domain_scoping_agent.is_complete(domain_scope):
            results["status"] = "failed"
            results["error"] = (
                f"Domain Scoping Agent failed after {MAX_AGENT_RETRIES} attempt(s). "
                f"Last error: {domain_scope.get('error', domain_scope.get('raw_output', 'unknown'))[:300]}"
            )
            return results

        # ── Step 4 — Silver Product Engine (retry handled by _retry_agent inside) ──
        product_plan = await _run_silver_product_engine(
            domain_scope, bank_profile, structured_req, session_id
        )
        results["steps"]["product_plan"] = product_plan

        if not silver_product_engine.is_complete(product_plan):
            results["status"] = "failed"
            results["error"] = (
                f"Silver Product Engine failed after {MAX_AGENT_RETRIES} attempt(s). "
                f"Last error: {product_plan.get('error', product_plan.get('raw_output', 'unknown'))[:300]}"
            )
            return results

        # ── Steps 5 + 6 — Spec Generator + Validator (coupled retry) ─────────
        spec_result = await _run_spec_generator_with_validation(
            product_plan, bank_profile, domain_scope, session_id
        )
        results["steps"]["spec"] = spec_result["spec"]
        results["steps"]["validation"] = spec_result["validation"]

        if not spec_generator_agent.is_complete(spec_result["spec"]):
            results["status"] = "failed"
            results["error"] = f"Spec Generator failed after {MAX_SPEC_ITERATIONS} attempt(s)."
            return results

        # ── Step 7 — BigQuery Publisher ───────────────────────────────────────
        ddl = spec_generator_agent.get_ddl(spec_result["spec"])
        pub_report = await asyncio.to_thread(_publish_sync, ddl, spec_result["spec"])
        results["steps"]["publish"] = pub_report

        results["status"] = "completed"
        results["published_tables"] = pub_report.get("published_tables", [])
        results["ddl_script"] = ddl
        results["specification"] = spec_generator_agent.get_specification(spec_result["spec"])

        _log(f"\n[Pipeline] ══ COMPLETE ══")
        _log(f"[Pipeline] Published tables: {results['published_tables']}")
        _log(f"[Pipeline] Publish status:   {pub_report.get('publish_status')}")
        _log(f"[Pipeline] Validation status:{spec_result['validation'].get('validation_status', 'N/A')}")

    except Exception as exc:
        results["status"] = "failed"
        results["error"] = str(exc)

    return results


def _publish_sync(ddl: str, spec: dict) -> dict:
    """Synchronous wrapper for publish (runs in thread pool)."""
    publisher = BigQueryPublisher()
    if not publisher.can_execute():
        return {
            "publish_status": "dry_run",
            "published_tables": [],
            "summary": "Dry run — set GCP_PROJECT_ID in .env and configure credentials to publish.",
        }
    return publisher.publish(ddl)


# ── CLI entrypoint ────────────────────────────────────────────────────────────

async def _main():
    print("=== BFSI Silver Schema Generator ===\n")

    if len(sys.argv) > 1:
        spec_path = sys.argv[1]
        try:
            with open(spec_path, encoding="utf-8") as f:
                data = json.load(f)
            bank_input = data.get("bank_profile", "")
            req_input = data.get("requirement", "")
            if isinstance(bank_input, dict):
                bank_input = json.dumps(bank_input)
            print(f"Loaded spec from: {spec_path}\n")
        except OSError as e:
            print(f"Error reading file: {e}")
            sys.exit(1)
    else:
        print("Describe your bank (name, regions, products, regulatory frameworks):")
        bank_input = input("> ").strip()
        print("\nDescribe the business requirement (what Silver data product do you need?):")
        req_input = input("> ").strip()

    if not bank_input or not req_input:
        print("Both bank description and requirement are needed. Exiting.")
        sys.exit(1)

    result = await run_pipeline(bank_input, req_input)

    print("\n=== Final Result ===")
    # Print a concise summary (not the full spec)
    summary = {
        "status": result.get("status"),
        "thread_id": result.get("thread_id"),
        "published_tables": result.get("published_tables", []),
        "publish_status": result.get("steps", {}).get("publish", {}).get("publish_status"),
        "validation_status": result.get("steps", {}).get("validation", {}).get("validation_status"),
        "error": result.get("error"),
    }
    print(json.dumps(summary, indent=2, default=str))

    # Save full output
    out_dir = Path(__file__).parent / "output"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"pipeline_{result['thread_id'][:8]}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, default=str)
    print(f"\nFull output saved to: {out_path}")


if __name__ == "__main__":
    asyncio.run(_main())
