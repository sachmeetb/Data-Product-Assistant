"""
pipeline.py — CLI runner for the AI Retail Data Agent pipeline.

Flow (natural language input):
  1. Orchestrator collects and extracts the spec from the user
  2. Pipeline Generator produces PySpark/DLT code
  3. Test Agent validates the code (loops up to MAX_ITERATIONS on failure)
  4. Publisher Agent writes tables to BigQuery

Flow (JSON spec input):
  Steps 2-4 only — orchestrator is skipped entirely.

Usage:
  cd backend
  python pipeline.py                     # interactive natural-language mode
  python pipeline.py spec.json           # read JSON spec from file
"""

import json
import sys
import uuid

from opentelemetry import trace

# Ensure UTF-8 output on Windows so generated code prints without errors
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from agents import orchestrator, pipeline_generator, test_agent, publisher_agent

tracer = trace.get_tracer("pipeline")

MAX_ITERATIONS = 5


def _log(msg: str, verbose: bool = True):
    if verbose:
        print(msg)


def _normalize_spec(spec: dict) -> dict:
    """
    Ensure the spec has a `target_tables` list the publisher can use.
    Supports both the orchestrator format (target_tables) and the direct
    JSON format (gold_schema.tables + silver in sttm mappings).
    """
    if spec.get("target_tables"):
        return spec

    target_tables = []

    # Gold tables from gold_schema.tables
    for t in (spec.get("gold_schema") or {}).get("tables", []):
        if t.get("name"):
            target_tables.append({
                "name": t["name"],
                "layer": "gold",
                "columns": t.get("columns", []),
                "description": t.get("description", ""),
            })

    # Silver tables inferred from sttm mappings
    seen = {t["name"] for t in target_tables}
    for mapping in (spec.get("sttm") or {}).get("mappings", []):
        tname = mapping.get("target_table", "")
        layer = mapping.get("layer", "silver")
        if tname and tname not in seen and layer == "silver":
            cols = [
                {"name": c["target"], "type": "STRING", "nullable": True}
                for c in mapping.get("columns", [])
            ]
            target_tables.append({"name": tname, "layer": "silver", "columns": cols})
            seen.add(tname)

    return {**spec, "target_tables": target_tables} if target_tables else spec


def _run_generator_test_publish(spec: dict, results: dict, verbose: bool = True) -> dict:
    """
    Shared inner loop: Pipeline Generator → Test Agent (retry) → Publisher Agent.
    Mutates and returns `results`.
    """
    generated_code = ""
    test_report: dict = {}
    feedback: dict | None = None

    for iteration in range(1, MAX_ITERATIONS + 1):
        _log(f"\n[Pipeline] Iteration {iteration}/{MAX_ITERATIONS} — Generating code...", verbose)

        gen_out = pipeline_generator.run(spec, test_feedback=feedback)
        results["steps"][f"pipeline_generator_iter_{iteration}"] = gen_out

        if "error" in gen_out or "raw_output" in gen_out:
            results["status"] = "failed"
            results["error"] = (
                f"Pipeline Generator failed (iter {iteration}): "
                + gen_out.get("error", gen_out.get("raw_output", ""))
            )
            _log(f"[Pipeline] ✗ {results['error']}", verbose)
            return results

        generated_code = gen_out.get("generated_code", "")
        if not generated_code:
            results["status"] = "failed"
            results["error"] = f"Pipeline Generator returned no code on iteration {iteration}."
            return results

        _log(
            f"[Pipeline]   Generated {len(generated_code)} chars"
            f" — target tables: {gen_out.get('target_tables', [])}",
            verbose,
        )
        print(f"\n--- Pipeline Generator Output (Iteration {iteration}) ---")
        print(generated_code)
        print("--- End Pipeline Generator Output ---\n")

        # ── Test Agent ────────────────────────────────────────────────────────
        _log(f"[Pipeline] Iteration {iteration}/{MAX_ITERATIONS} — Testing code...", verbose)

        test_report = test_agent.run(generated_code, spec)
        results["steps"][f"test_agent_iter_{iteration}"] = test_report

        if "error" in test_report or "raw_output" in test_report:
            results["status"] = "failed"
            results["error"] = (
                f"Test Agent failed (iter {iteration}): "
                + test_report.get("error", test_report.get("raw_output", ""))
            )
            _log(f"[Pipeline] ✗ {results['error']}", verbose)
            return results

        print(f"\n--- Test Agent Report (Iteration {iteration}) ---")
        print(f"Status : {test_report.get('test_status')}")
        print(f"Summary: {test_report.get('summary')}")

        if test_agent.is_passing(test_report):
            _log(f"[Pipeline] ✓ Tests passed on iteration {iteration}!", verbose)
            print("--- End Test Agent Report ---\n")
            break

        failures = test_agent.get_failures(test_report)
        print(f"Failures: {failures}")
        print("--- End Test Agent Report ---\n")
        _log(
            f"[Pipeline] ✗ Tests failed — {len(failures)} issue(s): {test_report.get('summary', '')}",
            verbose,
        )

        if iteration == MAX_ITERATIONS:
            results["status"] = "failed"
            results["error"] = f"Code did not pass validation after {MAX_ITERATIONS} attempts."
            results["last_test_report"] = test_report
            _log("[Pipeline] ✗ Max iterations reached — pipeline aborted.", verbose)
            return results

        feedback = {
            "failures": failures,
            "summary": test_report.get("summary", ""),
            "iteration": iteration,
        }

    # ── Publisher Agent ───────────────────────────────────────────────────────
    _log("\n[Pipeline] Step Final — Publishing to BigQuery...", verbose)

    pub_out = publisher_agent.run(generated_code, spec, test_report)
    results["steps"]["publisher"] = pub_out

    if "error" in pub_out or "raw_output" in pub_out:
        results["status"] = "failed"
        results["error"] = "Publisher Agent failed: " + pub_out.get(
            "error", pub_out.get("raw_output", "")
        )
        _log(f"[Pipeline] ✗ {results['error']}", verbose)
        return results

    published = pub_out.get("published_tables", [])
    _log(f"[Pipeline] ✓ Published: {published}", verbose)
    _log(f"[Pipeline] Summary   : {pub_out.get('summary', '')}", verbose)

    print("\n--- Publisher Agent Report ---")
    print(f"Status          : {pub_out.get('publish_status')}")
    print(f"Published Tables: {published}")
    print(f"Summary         : {pub_out.get('summary')}")
    print("--- End Publisher Agent Report ---\n")

    results["status"] = "completed"
    results["published_tables"] = published
    results["publish_report"] = pub_out

    _log("\n[Pipeline] ══════════════════════════════════════", verbose)
    _log(f"[Pipeline]  PIPELINE COMPLETE (thread: {results['thread_id']})", verbose)
    _log(f"[Pipeline]  Tables: {published}", verbose)
    _log("[Pipeline] ══════════════════════════════════════", verbose)

    return results


def run_pipeline(user_input: str, verbose: bool = True) -> dict:
    """
    Run the full pipeline from a raw user input string (natural language or JSON).

    - If `user_input` is valid JSON: skip the orchestrator and pass the spec
      directly to Pipeline Generator → Test Agent → Publisher Agent.
    - If `user_input` is natural language: run the orchestrator first to
      extract a structured spec, then proceed as above.

    Returns a consolidated result dict.
    """
    thread_id = str(uuid.uuid4())
    results = {
        "user_input": user_input[:200],
        "thread_id": thread_id,
        "status": "in_progress",
        "steps": {},
    }

    if not user_input or not user_input.strip():
        results["status"] = "failed"
        results["error"] = "No input provided."
        return results

    with tracer.start_as_current_span("pipeline-run") as root_span:
        root_span.set_attribute("pipeline.thread_id", thread_id)
        root_span.set_attribute("pipeline.user_input", user_input[:200])
        trace_id = format(root_span.get_span_context().trace_id, "032x")
        results["trace_id"] = trace_id
        _log(f"[Pipeline] Thread : {thread_id}", verbose)
        _log(f"[Pipeline] Trace  : {trace_id}", verbose)

        # ── Detect JSON spec — bypass orchestrator if provided ────────────────
        spec: dict | None = None
        try:
            parsed = json.loads(user_input)
            if isinstance(parsed, dict):
                spec = parsed
                _log("\n[Pipeline] JSON spec detected — skipping orchestrator.", verbose)
        except (json.JSONDecodeError, ValueError):
            pass

        # ── Step 0: Orchestrator (natural language path only) ─────────────────
        if spec is None:
            _log("\n[Pipeline] Step 0 — Orchestrator: collecting spec...", verbose)
            orch_out = orchestrator.run(user_input)
            results["steps"]["orchestrator"] = orch_out

            if "error" in orch_out or "raw_output" in orch_out:
                results["status"] = "failed"
                results["error"] = orch_out.get("error", orch_out.get("raw_output", "Orchestrator error"))
                _log(f"[Pipeline] ✗ Orchestrator error: {results['error']}", verbose)
                return results

            action = orch_out.get("action", "ask_user")
            conversation_history = [
                {"role": "user", "content": user_input},
                {"role": "assistant", "content": orch_out.get("reply", "")},
            ]

            while action == "ask_user":
                _log(f"\n[Orchestrator] {orch_out.get('reply', '')}", verbose)
                user_answer = input("> ").strip()
                if not user_answer:
                    _log("[Pipeline] No answer — aborting.", verbose)
                    results["status"] = "aborted"
                    return results

                conversation_history.append({"role": "user", "content": user_answer})
                orch_out = orchestrator.run(
                    user_answer, orch_out.get("pipeline_state"), conversation_history
                )
                results["steps"]["orchestrator"] = orch_out
                action = orch_out.get("action", "ask_user")
                conversation_history.append({"role": "assistant", "content": orch_out.get("reply", "")})

            if action != "start_pipeline":
                results["status"] = "failed"
                results["error"] = f"Orchestrator returned unexpected action '{action}'."
                return results

            spec = orch_out.get("extracted_spec", {})
            if not spec:
                results["status"] = "failed"
                results["error"] = "Orchestrator did not return a valid spec."
                return results

            _log(
                f"[Pipeline] ✓ Spec collected — domain: {spec.get('domain')}, "
                f"type: {spec.get('pipeline_type')}",
                verbose,
            )

        # ── Steps 1-N: Pipeline Generator → Test Agent → Publisher ────────────
        spec = _normalize_spec(spec)
        return _run_generator_test_publish(spec, results, verbose)


if __name__ == "__main__":
    print("=== AI Retail Data Agent — Pipeline Runner ===")

    # Accept a JSON file path as an optional argument
    if len(sys.argv) > 1:
        spec_path = sys.argv[1]
        try:
            with open(spec_path, encoding="utf-8") as f:
                raw = f.read()
            print(f"Reading spec from: {spec_path}\n")
        except OSError as e:
            print(f"Error reading file: {e}")
            sys.exit(1)
    else:
        print("Paste your pipeline spec (JSON or natural language).")
        print("Press Enter twice when done.\n")
        lines = []
        while True:
            try:
                line = input()
            except EOFError:
                break
            if line == "" and lines and lines[-1] == "":
                break
            lines.append(line)
        raw = "\n".join(lines).strip()

    if not raw:
        print("No input provided. Exiting.")
        sys.exit(1)

    result = run_pipeline(raw, verbose=True)

    print("\n=== Final Result ===")
    print(json.dumps(result, indent=2, default=str))
