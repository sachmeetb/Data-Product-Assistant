"""Non-LLM generator for DQ test code.

Runs the skill's generate_{gx,python}_tests.py as a subprocess against
the project's Neo4j, scoped to the project's code. Streams stdout
line-by-line so the Output tab shows exactly what the terminal would.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import AsyncGenerator

from .config import BASE_PROJECT_DIR, SKILLS_DIR
from .models import Project

# Generation is a quick Neo4j read + file write — 5 min is ample. Cap it so a
# stuck Neo4j connection can't leave the UI spinning forever.
_GEN_TIMEOUT_SECONDS = 5 * 60


_GENERATORS = {
    "gx": {
        "skill": "data-quality-testing-gx",
        "script": "generate_gx_tests.py",
        "output_dir": "dq_tests_gx",
    },
    "pandera": {
        "skill": "data-quality-testing-python",
        "script": "generate_python_tests.py",
        "output_dir": "dq_tests_python",
    },
}


def _generator_script_path(framework: str) -> Path | None:
    spec = _GENERATORS.get(framework)
    if not spec:
        return None
    p = SKILLS_DIR / spec["skill"] / "scripts" / spec["script"]
    return p if p.exists() else None


# Read the deployed product view's schema so dprod tests reference the right
# relations. deploy_virtual_view stamps sd.viewSchema (falls back to deployedTo);
# default 'public' when nothing is deployed yet. Best-effort — never raises.
_VIEW_SCHEMA_QUERY = """
MATCH (dc:DataContract {id: $contract_id})-[:MATERIALISES_AS]->(dp:DProdDataProduct)
MATCH (dp)-[:SERVED_BY]->(sd:ServingDefinition {servingMode: 'virtual_view'})
RETURN coalesce(sd.viewSchema, sd.deployedTo) AS view_schema
"""


def _resolve_view_schema(project: Project) -> str:
    try:
        from .neo4j_client import neo4j_session
        with neo4j_session(
            project.neo4j_host, project.neo4j_port,
            project.neo4j_user, project.neo4j_password, project.neo4j_database,
        ) as ns:
            row = ns.run(_VIEW_SCHEMA_QUERY,
                         contract_id=f"{project.project_code}-contract").single()
        if row and row.get("view_schema"):
            return row["view_schema"]
    except Exception:
        pass
    return "public"


async def generate_dq_tests(
    project: Project,
    framework: str = "gx",
    source_mode: str = "catalog",
    target_contract: str | None = None,
    view_schema: str | None = None,
) -> AsyncGenerator[dict, None]:
    """Generate test code for the given project + framework.

    ``source_mode='catalog'`` (default) mines :Column rules and targets the source
    DB — byte-identical to the historic path. ``source_mode='dprod'`` reads the
    product's contract rules on :DProdColumn and targets the deployed views
    (``target_contract`` = ``{project_code}-contract``; ``view_schema`` resolved
    from the :ServingDefinition when omitted).

    Yields streaming events (text_delta) and ends with stage_complete/error.
    """
    spec = _GENERATORS.get(framework)
    if spec is None:
        yield {"type": "error", "message": f"Unknown framework: {framework}"}
        return
    script = _generator_script_path(framework)
    if script is None:
        yield {
            "type": "error",
            "message": (
                f"Generator script not found for framework '{framework}'. "
                f"Expected at {SKILLS_DIR}/{spec['skill']}/scripts/{spec['script']}."
            ),
        }
        return

    project_dir = BASE_PROJECT_DIR / project.project_code
    project_dir.mkdir(parents=True, exist_ok=True)
    # dprod suites land in a `_dprod`-suffixed dir so an SA product can hold both
    # a catalog (source pre-check) and a dprod (deployed-product) suite without
    # the Builds clobbering each other on disk.
    from .dq_test_executor import dq_subdir
    output_dir = project_dir / dq_subdir(spec["output_dir"], source_mode)

    cmd = [
        sys.executable, "-u", str(script), str(output_dir),
        "--host", project.neo4j_host,
        "--bolt-port", str(project.neo4j_port),
        "--username", project.neo4j_user,
        "--password", project.neo4j_password,
        "--database", project.neo4j_database,
        "--project-code", project.project_code,
    ]
    if source_mode == "dprod":
        contract = target_contract or f"{project.project_code}-contract"
        vschema = view_schema or _resolve_view_schema(project)
        cmd += ["--source-mode", "dprod", "--target-contract", contract,
                "--view-schema", vschema]

    yield {"type": "text_delta", "text": f"▸ Generating {framework.upper()} DQ tests"
           f"{' (product / dprod mode)' if source_mode == 'dprod' else ''}\n  $ {' '.join(cmd)}\n"}

    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(project_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    assert proc.stdout is not None
    try:
        while True:
            line = await asyncio.wait_for(
                proc.stdout.readline(), timeout=_GEN_TIMEOUT_SECONDS
            )
            if not line:
                break
            yield {"type": "text_delta", "text": line.decode(errors="replace")}
        exit_code = await proc.wait()
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        yield {
            "type": "error",
            "message": f"Generator exceeded {_GEN_TIMEOUT_SECONDS // 60} min timeout; process killed.",
        }
        return

    if exit_code != 0:
        yield {
            "type": "error",
            "message": f"Generator exited with code {exit_code}.",
        }
        return

    yield {
        "type": "stage_complete",
        "cost_usd": 0.0,
        "duration_ms": 0,
        "session_id": None,
        "is_error": False,
    }
