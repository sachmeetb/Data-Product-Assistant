---
name: playbook-curator
description: Inspects and curates domain playbook state in Neo4j — lists playbook versions, compares baseline vs refined, and recommends which version to use. Use this skill when the user wants to check playbook status, compare versions, decide between baseline and refined playbooks, or audit the learning history of a domain.
---

# Playbook Curator

Queries domain playbook state from a Neo4j knowledge graph. Lists available playbook versions (baseline vs refined), shows item counts and summaries, and recommends which version to use based on learning history.

## When to Use

- Before running metadata enrichment or data mapping, to decide which playbook version to apply
- To audit the learning history of a domain after reflection cycles
- To compare what changed between playbook versions

## Prerequisites

- Domain and Playbook nodes must exist in Neo4j (created by the `playbook-reflector` skill)
- `neo4j` Python driver installed (`pip install neo4j`)

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/query_playbook_versions.py` | Query all playbook versions and items for a domain, output JSON summary |
| `scripts/recommend_version.py` | Analyze version history and recommend baseline vs refined |

All scripts support `--help`.

## Graph Model Reference

```
(:Domain {name: "Human Resources"})
  -[:HAS_PLAYBOOK]-> (:Playbook {phase: "enrichment", domain: "Human Resources"})
    -[:HAS_ITEM]-> (:PlaybookItem {rule: "...", version: 2, isCurrent: true})
    -[:HAS_VERSION]-> (:PlaybookVersion {
        version: 2,
        summary: "Added 3 rules for date formatting...",
        createdAt: <datetime>,
        itemsAdded: 3, itemsUpdated: 1, itemsRemoved: 0
    })
```

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

### Step 2 — Query playbook versions

```bash
python ${CLAUDE_SKILL_DIR}/scripts/query_playbook_versions.py \
  --domain "<domain>" \
  --host <host> --bolt-port <port> --username <user> --password <pass> --database <db> \
  --output playbook_status.json
```

Output structure:

```json
{
  "domain": "Human Resources",
  "phases": [
    {
      "phase": "enrichment",
      "item_count": 12,
      "latest_version": 2,
      "has_refined": true,
      "versions": [
        {
          "version": 2,
          "summary": "Added rules for date column formatting...",
          "created_at": "2026-04-05T14:30:00",
          "items_added": 3,
          "items_updated": 1,
          "items_removed": 0
        },
        {
          "version": 1,
          "summary": "Initial playbook",
          "created_at": "2026-04-01T10:00:00",
          "items_added": 8,
          "items_updated": 0,
          "items_removed": 0
        }
      ]
    }
  ]
}
```

### Step 3 — Get recommendation (optional)

```bash
python ${CLAUDE_SKILL_DIR}/scripts/recommend_version.py \
  --domain "<domain>" \
  --host <host> --bolt-port <port> --username <user> --password <pass> --database <db>
```

Output:

```json
{
  "recommendation": "refined",
  "version": 2,
  "rationale": "Refined playbook has 12 items across 2 reflection cycles. The latest cycle added 3 rules addressing date column formatting patterns identified from 5 review rejections."
}
```

### Step 4 — Report to user

Summarize the playbook state and recommendation. If recommending refined, explain what changed. If recommending baseline, explain why (e.g., no reflection cycles yet, or refined version is empty).
