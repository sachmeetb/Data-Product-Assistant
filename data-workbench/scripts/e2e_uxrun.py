"""Automated end-to-end UX run: Players Source + Teams Source + a Players⋈Teams
consumer, driven through the REAL MCP tool code path (what a persona session
calls). Front-loaded style: every input decided up front. Captures each tool
response's user-facing fields (next / status / error / produced), times each
step, and flags any INTERVENTION — a spot where the flow needed a human to
debug/work around (vs. a normal up-front input).

Run in the backend container:
    docker exec -e PYTHONPATH=/app data-workbench-backend-1 \
        python scripts/e2e_uxrun.py > /app/data/e2e_uxrun.out 2>&1

Reads its log back from /app/data/e2e_uxrun_log.json.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import warnings

logging.disable(logging.CRITICAL)
warnings.filterwarnings("ignore")

from sqlmodel import Session, select
from workbench.backend.database import engine
from workbench.backend.models import Project, StageRun
import workbench.backend.mcp_server as de
import workbench.backend.po_mcp_server as po

OWNER = "sodbayar.ganbat@accenture.com"
CONN = dict(host="host.docker.internal", database="lakers", username="lakers",
            password="lakers123", port=5432, schema_name="public")
LOG: list[dict] = []
_t0 = time.time()


def rec(persona, action, resp=None, elapsed=None, intervention=None):
    keys = ("next", "status", "error", "recommended_next", "produced", "approved",
            "blocked_by", "missing_config", "renamed_to", "count", "ready_to_submit", "gaps")
    slim = {k: resp[k] for k in keys if isinstance(resp, dict) and k in resp} if resp else {}
    entry = {"t": round(time.time() - _t0, 1), "persona": persona, "action": action,
             "resp": slim}
    if elapsed is not None:
        entry["stage_seconds"] = round(elapsed, 1)
    if intervention:
        entry["INTERVENTION"] = intervention
    LOG.append(entry)
    flag = f"  ⚠ INTERVENTION: {intervention}" if intervention else ""
    print(f"[{entry['t']:>6}s] {persona:2} {action}  -> {slim.get('error') or slim.get('status') or 'ok'}{flag}", flush=True)
    return resp


async def call(_mod, _fn, **kw):
    f = getattr(_mod, _fn)
    f = getattr(f, "fn", f)
    r = f(**kw)
    if asyncio.iscoroutine(r):
        r = await r
    return r


def stages(project_code):
    with Session(engine) as s:
        p = s.exec(select(Project).where(Project.project_code == project_code)).first()
        runs = s.exec(select(StageRun).where(StageRun.project_id == p.id)).all()
        return p.archetype, [(r.workflow_id, r.stage_number, r.stage_name,
                              r.status.value if hasattr(r.status, "value") else str(r.status))
                             for r in runs]


async def run_and_wait(persona, project_code, stage_number, workflow_id, config=None, timeout=360):
    t = time.time()
    r = await call(de, "run_stage", project_code=project_code, stage_number=stage_number,
                   workflow_id=workflow_id, config=config)
    if isinstance(r, dict) and r.get("error"):
        return rec(persona, f"run_stage #{stage_number} ({workflow_id})", r,
                   intervention=f"run_stage refused: {r['error'][:80]}")
    run_id = r.get("run_id") if isinstance(r, dict) else None
    last = None
    while time.time() - t < timeout:
        await asyncio.sleep(6)
        with Session(engine) as s:
            p = s.exec(select(Project).where(Project.project_code == project_code)).first()
            sr = s.exec(select(StageRun).where(StageRun.project_id == p.id,
                        StageRun.stage_number == stage_number,
                        StageRun.workflow_id == workflow_id)).first()
            last = (sr.status.value if hasattr(sr.status, "value") else str(sr.status))
            err = sr.error_message
        if last in ("complete", "awaiting_review", "failed"):
            break
    resp = {"status": last, "error": err if last == "failed" else None}
    return rec(persona, f"run_stage #{stage_number} ({workflow_id})", resp, elapsed=time.time() - t,
               intervention=(f"stage {last}: {err}" if last == "failed" else
                             ("stage did not finish (timeout)" if last not in ("complete", "awaiting_review") else None)))


async def drive_source(name, table):
    """PO creates a source; DE builds it end to end. Returns project_code."""
    r = await call(po, "create_source_product", owner_email=OWNER, owner_name="Sod",
                   idea=f"Expose our {table} table 1:1 as a clean governed source product.",
                   domain="sports", name=name)
    code = r.get("project_code")
    rec("PO", f"create_source_product({name})", r)
    if not code:
        return None
    await call(de, "accept_request", project_code=code)
    rec("DE", f"accept_request({code})")
    r = await call(de, "set_data_source", project_code=code, **CONN)
    rec("DE", "set_data_source", r)

    # Drive the pipeline by walking stages in order, dispatching by stage_id.
    attempts: dict = {}
    for _ in range(40):
        arche, st = stages(code)
        nxt = next(((wf, n, nm) for (wf, n, nm, status) in st
                    if status in ("pending", "failed")), None)
        if nxt is None:
            rec("DE", f"{name}: pipeline complete", {"status": "done"})
            break
        wf, n, nm = nxt
        key = (wf, n)
        attempts[key] = attempts.get(key, 0) + 1
        if attempts[key] > 2:
            rec("DE", f"{name}: STUCK at stage #{n} ({wf})", {"status": "stuck"},
                intervention=f"stage #{n} won't advance after {attempts[key]-1} attempts — abort")
            break
        sid = _resolve_sid(code, n, wf)
        if sid and sid.startswith("data_discovery"):
            await run_and_wait("DE", code, n, wf, config={"discovery_tables": f"public.{table}"})
        elif sid == "po_source_validation":
            r = await call(po, "bulk_approve_source_validation", project_code=code, tab="all")
            rec("PO", f"bulk_approve_source_validation({name})", r)
        elif sid == "deploy_virtual_view":
            r = await call(de, "deploy_virtual_view", project_code=code)
            rec("DE", "deploy_virtual_view", r,
                intervention=(r.get("error") if isinstance(r, dict) and r.get("error") else
                              (None if isinstance(r, dict) and r.get("status") == "deployed" else "deploy did not report 'deployed'")))
        elif sid == "deployment_reflection":
            # optional finisher — mark done to keep moving (skip the LLM reflection)
            await call(de, "complete_stage", project_code=code, stage_number=n, workflow_id=wf)
            rec("DE", "skip deployment_reflection (optional)")
        else:
            kind = _kind(code, n, wf, sid)
            if kind == "mechanical":
                r = await call(de, "complete_stage", project_code=code, stage_number=n, workflow_id=wf)
                rec("DE", f"complete_stage {sid}", r,
                    intervention=(r.get("error") if isinstance(r, dict) and r.get("error") else None))
            else:
                await run_and_wait("DE", code, n, wf)
    ok = _view_exists(f"vw_{table}")
    rec("DE", f"{name}: deployed view vw_{table} present = {ok}")
    return code, ok


def _view_exists(view_name):
    try:
        import psycopg2
        c = psycopg2.connect(host="host.docker.internal", port=5432, dbname="lakers",
                             user="lakers", password="lakers123")
        cur = c.cursor()
        cur.execute("select 1 from information_schema.views where table_schema='public' and table_name=%s", (view_name,))
        found = cur.fetchone() is not None
        c.close()
        return found
    except Exception:
        return False


def _resolve_sid(code, n, wf):
    from workbench.backend.routers.stages import _resolve_stage_id
    with Session(engine) as s:
        p = s.exec(select(Project).where(Project.project_code == code)).first()
        return _resolve_stage_id(p, n, wf, s)


def _kind(code, n, wf, sid):
    from workbench.backend.archetypes import STAGE_REGISTRY
    from workbench.backend.mcp_server import _execution_kind
    return _execution_kind(sid, STAGE_REGISTRY.get(sid or "", {}))


def _passthrough(name, src, lt="string", pk=False):
    return {"name": name, "logicalType": lt, "physicalType": lt, "primaryKey": pk,
            "transform": {"kind": "passthrough", "inputs": [src]}}


async def drive_consumer(players_code, teams_code):
    pc = f"{players_code}-contract"
    tc = f"{teams_code}-contract"
    r = await call(po, "start_consumer_product", owner_email=OWNER, owner_name="Sod",
                   product_idea="One trusted shared player roster everyone can use: name, position, "
                                "jersey, nationality, college, and current team name.",
                   domain="sports", name="Player Info Consumer")
    code = r.get("project_code")
    rec("PO", "start_consumer_product(Player Info Consumer)", r)
    if not code:
        return None
    spec = {
        "name": "Player Info Consumer", "domain": "sports",
        "purpose": "One trusted, shared roster everyone uses instead of their own lists.",
        "inputs": [{"contract_id": pc, "name": "Players Source"},
                   {"contract_id": tc, "name": "Teams Source"}],
        "schema": [{
            "name": "player_info_consumer", "physicalName": "player_info_consumer",
            "properties": [
                _passthrough("player_id", "player_id", "integer", pk=True),
                {"name": "first_name", "logicalType": "string", "transform": {"kind": "split", "inputs": ["full_name"], "params": {"part": "first"}}},
                {"name": "last_name", "logicalType": "string", "transform": {"kind": "split", "inputs": ["full_name"], "params": {"part": "rest"}}},
                _passthrough("position", "position"),
                _passthrough("nationality", "nationality"),
                _passthrough("college", "college"),
                _passthrough("jersey_number", "jersey_number"),
                _passthrough("team_id", "team_id", "integer"),
                {"name": "team_name", "logicalType": "string", "transform": {"kind": "lookup", "inputs": ["team_id"]}},
            ],
            "transform": {"grain_prose": "one row per player"},
        }],
    }
    r = await call(po, "save_product_spec", project_code=code, spec=spec)
    rec("PO", "save_product_spec(consumer)", r)
    cols = [{"name": p["name"], "logical_type": p.get("logicalType", "string")} for p in spec["schema"][0]["properties"]]
    r = await call(po, "run_gap_analysis", project_code=code, consumer_columns=cols)
    rec("PO", "run_gap_analysis", r)
    r = await call(po, "submit_product_spec", project_code=code, submitted_by=OWNER)
    rec("PO", "submit_product_spec(consumer)", r)

    # DE builds the consumer
    await call(de, "accept_request", project_code=code)
    rec("DE", f"accept_request({code})")
    await call(de, "set_data_source", project_code=code, **CONN)
    attempts: dict = {}
    for _ in range(25):
        arche, st = stages(code)
        nxt = next(((wf, n, nm) for (wf, n, nm, status) in st if status in ("pending", "failed")), None)
        if nxt is None:
            rec("DE", "consumer pipeline complete", {"status": "done"})
            break
        wf, n, nm = nxt
        key = (wf, n)
        attempts[key] = attempts.get(key, 0) + 1
        if attempts[key] > 2:
            rec("DE", f"consumer STUCK at stage #{n} ({wf})", {"status": "stuck"},
                intervention=f"stage #{n} won't advance — abort")
            break
        sid = _resolve_sid(code, n, wf)
        if sid == "data_mapping":
            await run_and_wait("DE", code, n, wf,
                               config={"data_product": "Player Info Consumer",
                                       "source_tables": "public.players, public.teams"})
            r = await call(de, "bulk_approve_mappings", project_code=code)
            rec("DE", "bulk_approve_mappings", r)
        elif sid == "serving_virtual_view":
            await run_and_wait("DE", code, n, wf)
        elif sid == "deploy_virtual_view":
            r = await call(de, "deploy_virtual_view", project_code=code)
            rec("DE", "deploy_virtual_view", r,
                intervention=(r.get("error") if isinstance(r, dict) and r.get("error") else None))
        elif sid == "deployment_reflection":
            await call(de, "complete_stage", project_code=code, stage_number=n, workflow_id=wf)
            rec("DE", "skip deployment_reflection (optional)")
        else:
            kind = _kind(code, n, wf, sid)
            if kind == "mechanical":
                r = await call(de, "complete_stage", project_code=code, stage_number=n, workflow_id=wf)
                rec("DE", f"complete_stage {sid}", r,
                    intervention=(r.get("error") if isinstance(r, dict) and r.get("error") else None))
            else:
                await run_and_wait("DE", code, n, wf)
    return code


async def main():
    print("=== E2E UX RUN START ===", flush=True)
    players, players_ok = await drive_source("Players Source", "players")
    teams, teams_ok = await drive_source("Teams Source", "teams")
    consumer = None
    if players_ok and teams_ok:
        consumer = await drive_consumer(players, teams)
    else:
        rec("--", "SKIP consumer — a source failed to build",
            {"players_ok": players_ok, "teams_ok": teams_ok},
            intervention="consumer skipped: source(s) not deployed")
    # Verify the consumer view actually serves data
    verify = {}
    try:
        import psycopg2
        c = psycopg2.connect(host="host.docker.internal", port=5432, dbname="lakers", user="lakers", password="lakers123")
        cur = c.cursor()
        cur.execute("select table_name from information_schema.views where table_schema='public' and table_name like 'vw_%'")
        verify["views"] = [r[0] for r in cur.fetchall()]
        try:
            cur.execute("select first_name,last_name,team_name from public.vw_player_info_consumer limit 3")
            verify["sample"] = cur.fetchall()
        except Exception as e:
            verify["sample_error"] = str(e)[:120]
        c.close()
    except Exception as e:
        verify["error"] = str(e)[:120]
    rec("--", "verify serving", verify)
    interventions = [e for e in LOG if "INTERVENTION" in e]
    summary = {"players": players, "teams": teams, "consumer": consumer,
               "total_seconds": round(time.time() - _t0, 1),
               "steps": len(LOG), "interventions": len(interventions)}
    print("=== SUMMARY ===", json.dumps(summary, indent=2), flush=True)
    print("=== INTERVENTIONS ===", flush=True)
    for e in interventions:
        print(f"  [{e['t']}s] {e['persona']} {e['action']}: {e['INTERVENTION']}", flush=True)
    with open("/app/data/e2e_uxrun_log.json", "w") as f:
        json.dump({"summary": summary, "log": LOG, "verify": verify}, f, indent=2)
    print("=== E2E UX RUN DONE ===", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
