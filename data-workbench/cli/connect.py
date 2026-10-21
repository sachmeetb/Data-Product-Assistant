"""Quick-connect provisioning manifest writer.

`dwb up` / `dwb connect` write repo-root `.dwb/provisioned-sources.json` — the
list of launched sample-DB coordinates the backend's dev router serves to the
frontend connection forms for one-click prefill. Demo creds only.
"""
from __future__ import annotations

import json

from . import samples, state

MANIFEST = state.DWB_DIR / "provisioned-sources.json"


def write_manifest(mode: str, profiles: list[str],
                   pg_selected: list[dict], mysql_selected: list[dict]) -> list[dict]:
    state.DWB_DIR.mkdir(parents=True, exist_ok=True)
    sources = samples.provisioned_sources(mode, profiles, pg_selected, mysql_selected)
    MANIFEST.write_text(json.dumps({"sources": sources}, indent=2) + "\n")
    return sources


def clear_manifest() -> None:
    try:
        MANIFEST.unlink()
    except FileNotFoundError:
        pass


def cmd_connect() -> int:
    """(Re)write the manifest for the current launch (from state.json)."""
    st = state.load_state()
    if not st:
        state.die("no active launch — run `dwb up` first (nothing to provision).")
    mode = st.get("mode", "compose")
    profiles = st.get("profiles", [])
    pg_selected = samples.resolve_selection("postgres", ",".join(state.state_samples(st, "postgres")) or None) \
        if "postgres" in profiles else []
    mysql_selected = samples.resolve_selection("mysql", ",".join(state.state_samples(st, "mysql")) or None) \
        if "mysql" in profiles else []
    sources = write_manifest(mode, profiles, pg_selected, mysql_selected)
    if not sources:
        state.warn("no sample DBs are running — nothing to prefill "
                   "(launch with `dwb up --with postgres,mysql`).")
        return 0
    state.info(f"Wrote {MANIFEST.relative_to(state.ROOT)} ({len(sources)} source(s)):")
    for s in sources:
        state.ok(f"{s['name']}  {s['platform']}  {s['host']}:{s['port']}/{s['database']} "
                 f"(schema {s['schema']})")
    print(f"\n  These appear as a {state.GREEN}Quick connect{state.NC} section in the "
          f"project data-source dialog and the Connections page.")
    return 0
