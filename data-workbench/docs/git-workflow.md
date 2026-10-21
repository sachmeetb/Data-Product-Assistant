# Git Workflow — Data Workbench

Conventions and commands for day-to-day development on the `data-workbench` repo hosted on Azure DevOps.

---

## Remote

```
https://cf-incubation@dev.azure.com/cf-incubation/Data%20Workbench/_git/data-workbench
```

The canonical branch is **`main`**. A `master` branch exists locally but is not tracked upstream — work off `main`.

---

## 1. Clone

```bash
# HTTPS (prompts for your Azure DevOps personal access token on first use)
git clone https://cf-incubation@dev.azure.com/cf-incubation/Data%20Workbench/_git/data-workbench
cd data-workbench
```

**Azure DevOps credentials**

Azure DevOps does not use your normal AD password for Git HTTPS. Generate a **Personal Access Token (PAT)**:

1. Sign in to `dev.azure.com/cf-incubation`
2. Top-right avatar → **Personal access tokens** → **+ New Token**
3. Scopes: **Code (Read & Write)** is sufficient; **Code (Full)** if you need to manage branches/PRs from the CLI.
4. Set an expiry, copy the token — it will not be shown again.
5. When Git asks for a password, paste the PAT (your username is your email or the `cf-incubation` org alias shown in the URL).

**Storing the credential** (so you are not asked every push):

```bash
# Linux — use the libsecret credential helper (requires gnome-keyring or equivalent)
git config --global credential.helper /usr/lib/git-core/git-credential-libsecret

# WSL2 — delegate to the Windows Credential Manager
git config --global credential.helper "/mnt/c/Program\ Files/Git/mingw64/bin/git-credential-manager.exe"

# Fallback — cache in memory for 8 hours
git config --global credential.helper "cache --timeout=28800"
```

---

## 2. Staying current with `main`

```bash
git checkout main
git pull --ff-only origin main
```

`--ff-only` fails if your local `main` has diverged, which is a signal to investigate before proceeding.

---

## 3. Feature branches

All work happens on a short-lived feature branch off `main`. Never commit directly to `main`.

```bash
# Create and switch to a new branch
git checkout -b feat/your-short-description

# Or, equivalently with newer git
git switch -c feat/your-short-description
```

**Branch naming conventions**

| Prefix | Use for |
|---|---|
| `feat/` | New feature or capability |
| `fix/` | Bug fix |
| `refactor/` | Code restructuring, no behaviour change |
| `docs/` | Documentation only |
| `chore/` | Build, CI, dependency updates |

Keep names short and lowercase with hyphens: `feat/semantic-qa-explain-trace`, not `Feature_SemanticQA_ExplainTrace`.

---

## 4. Making commits

```bash
# Always check what you are staging — avoid committing .env, secrets, or unrelated drift
git status
git diff

# Stage specific files (preferred over git add .)
git add workbench/backend/some_module.py
git add workbench/frontend/src/SomeComponent.tsx

# Commit
git commit -m "Add semantic QA explain-trace endpoint"
```

**Commit message style** (match `git log --oneline`):

- Short imperative title, ~50 characters, no trailing period.
- If a body is needed, leave one blank line after the title, then wrap at 72 characters.
- Trailer for AI-assisted commits: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`

```
Add semantic QA explain-trace endpoint

Wires the /explain route through the retrieval layer so the UI
can surface provenance without a full re-query.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

**What NOT to commit**

- `.env` (gitignored — contains API keys)
- Files under `projects/` (runtime scratch)
- Unrelated modifications in `samples/` or `research/` (pre-existing working-tree drift)

---

## 5. Pushing and opening a Pull Request

```bash
# First push — set upstream tracking
git push -u origin feat/your-short-description

# Subsequent pushes on the same branch
git push
```

Open a PR on Azure DevOps:
1. Go to `dev.azure.com/cf-incubation/Data Workbench/_git/data-workbench/pullrequests`
2. Click **New pull request** — source: your feature branch, target: `main`.
3. Title follows the same commit style (imperative, ≤70 characters).
4. Add a brief description covering *why*, not just *what*.
5. Request at least one reviewer before merging.

**Never force-push to `main`.** If you need to rewrite history on your own feature branch (e.g., to squash before merge), confirm with your team first.

---

## 6. Common day-to-day operations

```bash
# See all local and remote branches
git branch -a

# Switch to an existing branch
git checkout feat/existing-branch
# or
git switch feat/existing-branch

# Pull in the latest main while on your feature branch (rebase keeps history clean)
git fetch origin
git rebase origin/main

# Resolve conflicts after rebase, then continue
git add <resolved-files>
git rebase --continue

# Abort a rebase if things go wrong
git rebase --abort

# Discard local unstaged changes to a specific file (destructive — data is lost)
git checkout -- path/to/file

# Stash work in progress before switching branches
git stash push -u -m "wip: describing what you were doing"
git stash pop     # restore later

# View what changed between your branch and main
git diff origin/main...HEAD

# Delete a branch after merge
git branch -d feat/your-short-description         # local
git push origin --delete feat/your-short-description  # remote
```

---

## 7. Tips specific to this repo

- **`samples/` and `research/` drift.** These directories often have pre-existing uncommitted modifications. After a broad `git status`, ignore them unless you deliberately changed something there. Use explicit `git add <file>` so they don't sneak into your commit.
- **Backend and frontend are in the same repo.** A single PR can span both; just make sure `npx tsc --noEmit` passes before pushing (see `CLAUDE.md` → "Verifying TS changes").
- **Skills are source files.** Edits under `workbench-skills/skills/` are normal commits — they ship with the change that needs them.
- **`.env` is gitignored.** If you are setting up a fresh clone, re-create it from the template in `docs/getting-started.md` — it is never in the repo.
