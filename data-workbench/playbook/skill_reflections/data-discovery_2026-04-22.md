# Skill Reflection: data-discovery
Generated: 2026-04-22
Runs analyzed: 2 across 1 project (dpe-04222026-01)

Signal is thin (only 2 runs, 1 project) — patterns are tentative and should be re-validated once more projects run. That said, both runs exhibit the same two behaviors, so the signal is internally consistent.

## Aggregate metrics
- Duration (p50 / p90): 43.9s / 51.9s
- Cost (p50 / p90): $0.467 / $0.468
- Event count (p50 / p90): 19 / 19 — tight
- Failure rate: 0% · Truncation rate: 0% · Agent-question rate: 0%
- Top tools: Bash (16 total, median 8/run), Skill (4 total, median 2/run)
- Heuristic flags: `bash_exploration=3` in both runs, every other flag at zero

Self-clarification, agent questions, and truncation are all clean — the current anti-exploration directive and the prompt_templates are doing their job for those dimensions. Budget and duration are also stable across runs.

## Observed patterns

### Pattern: wrong-skill-slug retry on the Neo4j load step
Evidence: 2 of 2 runs (runs `5c6ffb0b-98a6-4573-ac04-b150a791d8a0` and `49deae98-7634-4243-b3ac-a4fa9b835844`). Each run invokes the cypher generator at a shortened skill slug that does not exist, hits an error, `ls`es the correct skill directory, then retries with the full slug.

Quotes:
> "python ~/.claude/skills/dcat-neo4j/scripts/generate_cypher.py /home/niel/working/claudecodedash/projects/dpe-04222026-01/data_discovery" (run 5c6ffb0b-98a6-4573-ac04-b150a791d8a0)
> "ls /home/niel/.claude/skills/data-discovery-to-dcat-neo4j/scripts/" (run 5c6ffb0b-98a6-4573-ac04-b150a791d8a0)
> "python ~/.claude/skills/dcat-neo4j/scripts/generate_cypher.py data_discovery data_discovery/catalog.cypher --project-code dd-04222026-01" (run 49deae98-7634-4243-b3ac-a4fa9b835844)
> "ls ~/.claude/skills/data-discovery-to-dcat-neo4j/scripts/" (run 49deae98-7634-4243-b3ac-a4fa9b835844)

Rationale: the agent guesses a shorter form of the load-skill's slug (`dcat-neo4j` vs the actual `data-discovery-to-dcat-neo4j`) and has to recover via `ls`. Two wasted Bash calls and one failed script invocation per run. The `data_discovery_composite` prompt_template names Step 2 only as "load the discovered schema metadata to the Neo4j knowledge graph" — it doesn't pin the skill identity, so the agent falls back to pattern-matching the skill name.

### Pattern: post-extraction directory flattening
Evidence: 2 of 2 runs. After `extract_metadata.py` completes, the agent `ls`es the output directory, discovers the YAMLs are nested under `data_discovery/data_discovery/`, then moves them up one level. This accounts for the `bash_exploration=3` heuristic in both runs.

Quotes:
> "ls /home/niel/working/claudecodedash/projects/dpe-04222026-01/data_discovery/" (run 5c6ffb0b-98a6-4573-ac04-b150a791d8a0)
> "ls /home/niel/working/claudecodedash/projects/dpe-04222026-01/data_discovery/data_discovery/" (run 5c6ffb0b-98a6-4573-ac04-b150a791d8a0)
> "mv /home/niel/working/claudecodedash/projects/dpe-04222026-01/data_discovery/data_discovery/*.yaml /home/niel/working/claudecodedash/projects/dpe-04222026-01/data_discovery/ && rmdir /home/niel/wor..." (run 5c6ffb0b-98a6-4573-ac04-b150a791d8a0)
> "ls -la data_discovery/" (run 49deae98-7634-4243-b3ac-a4fa9b835844)
> "ls -la data_discovery/data_discovery/" (run 49deae98-7634-4243-b3ac-a4fa9b835844)
> "mv data_discovery/data_discovery/*.yaml data_discovery/ && rmdir data_discovery/data_discovery && ls data_discovery/" (run 49deae98-7634-4243-b3ac-a4fa9b835844)

Rationale: `extract_metadata.py` is called with `"data_discovery"` as its `output_dir` argument and writes into `<output_dir>/data_discovery/` — a nested layout the agent has to correct before Step 2 can pick up the YAMLs. This is a genuine bug in the script's output layout, not agent wandering; but until the script is fixed, the `ls` exploration is unavoidable. The SKILL.md does not warn about this, so each run rediscovers it.

## Proposed changes

### Change 1 — archetypes.py prompt_template (`data_discovery_composite`): name the Step 2 skill explicitly
Target: archetypes.py prompt_template (`data_discovery_composite`)
Current:
> "Step 2: After discovery is complete, load the discovered schema metadata to the Neo4j knowledge graph. Process all YAML files in the data_discovery directory. Do not ask which tables — load all discovered files. Use --project-code {project_code} for all script invocations to scope data to this project."
Proposed:
> "Step 2: After discovery is complete, load the discovered schema metadata to the Neo4j knowledge graph using the `data-discovery-to-dcat-neo4j` skill. Invoke its scripts at `~/.claude/skills/data-discovery-to-dcat-neo4j/scripts/generate_cypher.py` and `~/.claude/skills/data-discovery-to-dcat-neo4j/scripts/run_cypher.py` — do not shorten the skill slug. Process all YAML files in the data_discovery directory. Do not ask which tables — load all discovered files. Use --project-code {project_code} for all script invocations to scope data to this project."
Rationale: pinning the literal skill slug eliminates the `dcat-neo4j` guess observed in both runs (`5c6ffb0b`, `49deae98`). Same fix should be applied to `data_profiling_composite` if the next reflection shows the same pattern for `data-profiling-to-dqv-neo4j`.

### Change 2 — SKILL.md (`data-discovery`): document the nested output layout in Step 6
Target: SKILL.md (`data-discovery`), "Step 6: Extract metadata and write YAML"
Current:
> "- `output_dir` is the user's current working directory
> - Each table is passed as `schema.table` (e.g. `employees.salary`)
> - One YAML file per table is written as `<schema>__<table>.yaml`"
Proposed:
> "- `output_dir` is the user's current working directory
> - Each table is passed as `schema.table` (e.g. `employees.salary`)
> - One YAML file per table is written as `<schema>__<table>.yaml`
> - The script currently writes the YAMLs into a nested `<output_dir>/data_discovery/` subdirectory. Before proceeding, flatten them with `mv <output_dir>/data_discovery/*.yaml <output_dir>/ && rmdir <output_dir>/data_discovery`. Do not `ls` first — the nesting is always present."
Rationale: both runs `ls`ed the output directory twice to discover the nesting before moving files up. Documenting the known layout removes two Bash exploration calls per run. A durable fix would change `extract_metadata.py` to write directly into `output_dir` — when that lands, this SKILL.md note should be removed.

---

## Applying the proposals

1. Edit `workbench/backend/archetypes.py` — update the `data_discovery_composite` `prompt_template` per Change 1. Commit to the repo.
2. Edit `~/.claude/skills/data-discovery/SKILL.md` — update Step 6 per Change 2. Skills live outside the repo, so this is a manual edit on disk, not a commit.
3. Run a new project through Data Discovery and reflect again — confirm the `bash_exploration` heuristic drops toward zero and the wrong-skill retry loop disappears.
