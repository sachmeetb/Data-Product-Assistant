---
name: data-transform-translation
description: Authoring corpus + build tooling for the cross-platform transform capability artifact. Declares, per served platform (postgres/databricks/snowflake/bigquery/mysql), which SQL functions and neutral transform ops are native / emulated / unsupported, with an emission rule, remediation, engine-version assumptions, and a doc citation for each. A validated build step compiles the per-platform YAML into a single checksummed runtime artifact (platform/transform_capabilities.<schemaver>.json) that the backend transform compiler + preflight read as the sole authority. Trigger when adding/forking a platform's translation rules, seeding a new transform op, or after editing any reference/<platform>/transforms.yaml (then rebuild + validate). This corpus is CLIENT-FORKABLE: a client edits the YAML, rebuilds, validates, and promotes a new artifact without touching backend code.
---

# Data Transform Translation

This skill is the **single authoring source** for cross-platform transform
portability. It does two jobs:

1. **Declares capabilities** — for every SQL function and neutral transform op
   the Workbench can emit, it records per served platform whether the platform
   supports it `native`ly, only via an `emulated` rewrite, or `unsupported`.
2. **Builds the runtime artifact** — a validated build step compiles the
   per-platform YAML into ONE checksummed JSON artifact that the backend reads.

**The backend never imports this skill.** The compiler and preflight read only
the generated artifact at `workbench/backend/platform/transform_capabilities.<schemaver>.json`.
This skill is where a human (Accenture or a client) *authors* the rules; the
artifact is what *runs*. Keeping them separate means the runtime does not depend
on whether the skill is installed, and a client fork is a data change, not a
code change.

## Why this exists (read first)

`sqlglot.transpile` will happily re-emit `AGE(...)` for Databricks and
`SPLIT_PART(...)` for BigQuery/MySQL **unchanged** — it accepts syntax without
proving the target engine supports it. Transpile-success is **not** capability
validation. So we walk the parsed AST and check every function against *this*
artifact, failing closed on anything unknown or marked `unsupported`.

Two semantic traps this corpus is designed to prevent:

- **`AGE(end, start)`** in Postgres returns a *symbolic interval* (years + months
  + days), not a scalar. `DATEDIFF(end, start)` on Databricks returns *days*;
  BigQuery `DATE_DIFF(end, start, YEAR)` counts *calendar-year boundaries*, not
  *completed elapsed years*. A syntactically-valid translation can silently
  change the business answer. That is why the `date_difference` op carries an
  explicit `semantics` discriminator (`completed_units | boundary_count |
  symbolic_interval`) and each rendering is qualified.
- **Candidate renderings are marked `status: conformance-verify`** until a real
  engine has executed them. We never assert a rendering is correct from memory.

## Layout

```
data-transform-translation/
  SKILL.md                              ← this file
  schema/
    transforms_source.schema.json       ← validates each reference/<platform>/transforms.yaml
    transform_capabilities.schema.json  ← validates the GENERATED runtime artifact
  reference/
    portable_functions.yaml             ← ANSI-safe functions supported on ALL served platforms
    postgres/transforms.yaml
    databricks/transforms.yaml
    snowflake/transforms.yaml
    bigquery/transforms.yaml
    mysql/transforms.yaml
  scripts/
    build_artifact.py                   ← the validated build step (YAML → JSON artifact)
```

## Capability model

Each `(function | op, platform)` entry carries:

| field | meaning |
|---|---|
| `capability` | `native` \| `emulated` \| `unsupported` |
| `emit` | the SQL rule/template the renderer should use (informational for v1) |
| `remediation` | what to do when `unsupported` (e.g. "decompose to `date_difference` with `symbolic_interval`") |
| `engine` | the engine/version the claim assumes (e.g. "Databricks Runtime 13+ / Spark 3.4") |
| `status` | `verified` \| `conformance-verify` — `conformance-verify` = candidate, not yet run on a real engine |
| `citation` | a doc URL grounding the claim |
| `notes` | any semantic caveat (null handling, timezone, overflow) |

`native` and `emulated` both mean **supported** (the AST validator passes them);
`unsupported` and *absent-from-every-map* both mean **fail closed** (the
validator raises an error). A function that is neither in `portable_functions`
nor in any platform's map is treated as **unknown → error**.

## Authoring workflow

1. Edit `reference/<platform>/transforms.yaml` (or `portable_functions.yaml`).
2. Rebuild + validate:

   ```bash
   python scripts/build_artifact.py \
       --out ../../../workbench/backend/platform/transform_capabilities.v1.json
   ```

   The build step validates every source YAML against
   `schema/transforms_source.schema.json`, merges them, inverts them into the
   function/op maps, stamps `schema_version` + a content `checksum`, and
   validates the result against `schema/transform_capabilities.schema.json`.
   It **fails closed**: a malformed YAML, an unknown capability value, or a
   schema violation aborts the build and writes nothing.
3. Run the backend guard: `env/bin/python -m pytest workbench/backend/tests/test_transform_capabilities_artifact.py`.
4. Commit both the YAML and the regenerated artifact.

## Client fork → rebuild → validate → promote → rollback

A client that runs a different engine version (or wants a different emission
rule) forks this skill:

1. **Fork** the `reference/<platform>/transforms.yaml` entries they want to change.
2. **Rebuild** with `build_artifact.py` (bump the `--schema-version` only for a
   breaking shape change; a rule/capability change keeps the same version and
   changes the checksum).
3. **Validate** — the backend test asserts the artifact matches its schema and
   its self-declared checksum, and that every served platform is covered.
4. **Promote** — replace `workbench/backend/platform/transform_capabilities.<ver>.json`.
   The backend picks the highest `schema_version` file it finds.
5. **Rollback** — restore the previous artifact file; nothing else changed, so
   the compiler reverts deterministically.

The runtime artifact is a build output; treat it like compiled code — never hand-edit it.
