# Branching Strategy

## Branch Model

```
main          ← stable, production-ready only
develop       ← integration branch, unstable/R&D work lands here
feature/*     ← individual features/experiments, branch off develop
hotfix/*      ← critical fixes — branch off main, merge to main + develop
```

## Rules

| Branch | Purpose | Branch from | Merges into |
|---|---|---|---|
| `main` | Production-ready code | — | — |
| `develop` | Ongoing integration, R&D | `main` | `main` (via PR) |
| `feature/*` | New features or experiments | `develop` | `develop` (via PR) |
| `hotfix/*` | Critical production fixes | `main` | `main` + `develop` |

## Day-to-Day Flow

### Starting new work
```bash
git checkout develop
git pull origin develop
git checkout -b feature/my-thing
```

### Finishing a feature
```bash
# Push and open a PR into develop
git push -u origin feature/my-thing
```

### Releasing to main
When `develop` is stable enough to ship, open a PR from `develop` → `main`.

### Hotfix
```bash
git checkout -b hotfix/critical-fix main
# ... fix ...
# PR into main, then backmerge into develop
git checkout develop
git merge hotfix/critical-fix
```

## Conventions

- Commit titles: short imperative (`add`, `fix`, `update`) — no trailing period
- Never force-push `main` or `develop`
- Never push with `--no-verify`
- Prefer explicit `git add <file>` over `git add .` to avoid committing `samples/` or `research/` drift
- R&D / research changes live in `develop` or a `feature/` branch — never directly on `main`

## Azure DevOps Recommendations

- Protect `main`: require PR + at least one reviewer
- Optionally protect `develop`: require PR, no reviewer required (solo or small team)
