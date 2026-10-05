# Playbook Reflector

Analyzes review outcomes (approved/rejected descriptions and mappings) within a domain and generates or updates domain-scoped playbook rules that improve future metadata enrichment and data mapping.

## When to Use

Use this skill after one or more review cycles have completed (descriptions reviewed, mappings reviewed) for a project that has a domain assigned. The reflector examines patterns in review outcomes and generates actionable playbook rules.

## Prerequisites

- Project must have a domain assigned
- At least one review cycle must have completed (approved/rejected descriptions or mappings in the graph)
- Domain node must exist in Neo4j

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/query_review_outcomes.py` | Query Neo4j for all review outcomes (rejections with corrections, approvals with quality scores, aggregate stats) |
| `scripts/write_playbook_items.py` | Write PlaybookItem nodes to Neo4j with PROV-O provenance (add, update, merge, delete operations) |
| `scripts/write_playbook_version.py` | Create a PlaybookVersion node to track the reflection cycle with item counts and narrative summary |

All scripts support `--help`.

**Dependency:** all scripts require the `neo4j` Python driver — install with `pip install neo4j`.

## Graph Model

### Nodes Created/Updated

```
(:Domain {name: "Human Resources"})
  -[:HAS_PLAYBOOK]-> (:Playbook {
      uri:   "playbook:<domain>:<phase>",
      phase: "enrichment" | "mapping",
      domain: "<domain name>"
  })
    -[:HAS_ITEM]-> (:PlaybookItem {
        uri:       "playbook-item:<domain>:<phase>:<sequence>:<version>",
        rule:      "When generating descriptions for date columns in HR, always include the employment event context.",
        version:   1,
        isCurrent: true
    })
```

### Provenance on Playbook Items

```
(:PlaybookItem)
  -[:PROV_WAS_GENERATED_BY]-> (:ProvActivity {
      uri:          "prov:activity:playbook-curation:<domain>:<timestamp>",
      activityType: "playbook_curation",
      operation:    "add" | "update" | "merge" | "delete",
      rationale:    "Why this rule was created/updated, grounded in review evidence",
      occurredAt:   "<ISO timestamp>"
  })
  -[:PROV_WAS_ASSOCIATED_WITH]-> (:ProvAgent {
      uri:       "prov:agent:ai:playbook-reflector",
      agentType: "ai",
      name:      "playbook-reflector"
  })

// Updated items link to their predecessor
(:PlaybookItem {isCurrent: true})
  -[:PROV_WAS_DERIVED_FROM]-> (:PlaybookItem {isCurrent: false})

// Curation activity links back to the review evidence that informed it
(:ProvActivity {activityType: "playbook_curation"})
  -[:PROV_INFORMED_BY]-> (:ProvActivity {activityType: "review"})
```

### Playbook Version Tracking

```
(:Playbook)
  -[:HAS_VERSION]-> (:PlaybookVersion {
      version:      2,
      summary:      "Added rules for date column formatting based on 5 rejections...",
      createdAt:    <datetime>,
      itemsAdded:   3,
      itemsUpdated: 1,
      itemsRemoved: 0
  })
```

Each reflection cycle increments the version and records what changed.

## Workflow

### Step 1 — Get connection details

If not already provided, the following defaults apply:

| Setting | Default |
|---------|---------|
| Host | `localhost` |
| Bolt port | `7687` |
| Username | `neo4j` |
| Password | `your_password` |
| Database | `neo4j` |

### Step 2 — Query review outcomes

Run the query script to gather all review data for the domain:

```bash
python ${CLAUDE_SKILL_DIR}/scripts/query_review_outcomes.py \
  --domain "<domain>" \
  --host <host> --bolt-port <port> --username <user> --password <pass> --database <db> \
  --output playbook/review_outcomes.json
```

This produces a JSON file containing:
- All rejected descriptions with: original text, corrected text, rejection category, detail, column name, table, data type
- All approved descriptions with: quality score (1-3), text, column name, table, data type
- All rejected mappings with: category, detail, source column, original target, remapped target
- All approved mappings with: quality score, source column, target column, similarity score
- Aggregate statistics: rejection rate, most common rejection categories, quality score distribution

### Step 3 — Query existing playbook

```bash
python ${CLAUDE_SKILL_DIR}/scripts/query_review_outcomes.py \
  --domain "<domain>" --existing-playbook \
  --host <host> --bolt-port <port> --username <user> --password <pass> --database <db> \
  --output playbook/existing_playbook.json
```

If this is the first run, the existing playbook will be empty.

### Step 4 — Analyze patterns and generate playbook items

Read the review outcomes JSON file (`playbook/review_outcomes.json`) and the existing playbook JSON file (`playbook/existing_playbook.json`). Analyze the patterns:

1. **Rejection patterns**: Group rejections by category. For each category with 2+ occurrences, identify what the corrections have in common.
2. **Quality-3 exemplars**: Extract descriptions/mappings rated "Excellent" as positive examples. What makes them good? Generalize that into a rule.
3. **Quality-1 patterns**: Identify approved-but-marginal items that suggest improvement areas.
4. **Column type patterns**: Look for patterns by data type (date columns, FK columns, enum columns, identifier columns, etc.).

Generate playbook items as a JSON array and write to `playbook/playbook_items.json`:

```json
[
  {
    "phase": "enrichment",
    "operation": "add",
    "rule": "The rule text — generic, no specific column names or tables",
    "rationale": "Evidence-grounded explanation of why this rule was generated",
    "evidence_activities": ["prov:activity:...", "prov:activity:..."]
  }
]
```

### Rules for Playbook Items

- **Generic**: Rules must NOT reference specific column names, table names, or project-specific details. They should be reusable across any dataset in the domain.
- **Actionable**: Each rule should clearly describe what to do or avoid when generating descriptions/mappings.
- **Evidence-grounded**: The rationale must cite the specific patterns that motivated the rule.
- **Max 30 items per playbook** (per phase). If at capacity, merge redundant items or remove least-useful ones.
- **Operations**: `add` (new rule), `update` (refine existing rule, cite predecessor), `merge` (combine two rules), `delete` (remove harmful rule).

### Step 5 — Write playbook items to Neo4j

```bash
python ${CLAUDE_SKILL_DIR}/scripts/write_playbook_items.py \
  playbook/playbook_items.json \
  --domain "<domain>" \
  --host <host> --bolt-port <port> --username <user> --password <pass> --database <db>
```

### Step 5.5 — Write PlaybookVersion and reflection summary

After writing playbook items, generate a reflection summary and create a version record:

1. Write `reflection_summary.json` to the current directory:

```json
{
  "narrative": "Human-readable explanation of what changed and why, grounded in the review evidence",
  "items_added": 3,
  "items_updated": 1,
  "items_removed": 0,
  "key_patterns": ["date columns need employment event context", "FK columns should reference parent table"]
}
```

2. Run the version script:

```bash
python ${CLAUDE_SKILL_DIR}/scripts/write_playbook_version.py \
  reflection_summary.json \
  --domain "<domain>" \
  --host <host> --bolt-port <port> --username <user> --password <pass> --database <db>
```

This creates a `:PlaybookVersion` node for each phase playbook, linked via `[:HAS_VERSION]`, with auto-incremented version number.

### Step 6 — Report

After writing, report:
- How many items were added/updated/merged/deleted
- The current playbook item count per phase
- Key patterns identified from the review data
