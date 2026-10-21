# Advisor Models for Data Workbench

*Status: decision pending — see [open-items.md](open-items.md). Full analysis in [research/2026-07-13-advisor-models-data-workbench.md](../research/2026-07-13-advisor-models-data-workbench.md).*

---

## What it is (simply)

Every time a pipeline stage runs, Claude starts completely fresh. It doesn't know what corrections were made in previous runs — which column naming patterns this domain's reviewers always reject, which transform kinds a particular project's engineer replaces, what quirks this schema has. Engineers approve, reject, and replace AI suggestions every day, and all of that feedback is already stored in the system (rejection reasons, quality ratings, replacement patterns). But none of it flows back into the next run.

An **advisor model** solves this by adding a coach step before each run: read the domain's correction history, then inject a few specific tips into the stage prompt before Claude starts. The AI doesn't change — it just gets better prep for *this specific job*.

---

## The gap this addresses

The existing **reflection loop** already tries to learn from corrections — but it's slow (days to weeks), human-gated, and produces generic rules ("always do X in domain Y"). It cannot produce run-specific guidance ("for *this* project, *this* reviewer tends to correct X").

No static prompt or playbook can express per-instance guidance. That's the advisor's specific value.

---

## Why the data mapping stage is the right starting point

- It's the most expensive stage for human review time.
- Every correction is already stored with a structured category and reason (`:ProvRejectionReason`).
- It's the stage where Claude's defaults visibly get overridden by humans — meaning there's something to learn.

---

## Staged approach

| Phase | What | Effort | Gate |
|---|---|---|---|
| **0** | Measure baseline first-pass approval rates and rejection categories per domain — no code changes, data already exists | days | Go/no-go for Phase 1 |
| **1** | Add `{advice}` channel to `build_prompt()` + a `mapping-advisor` skill (one-shot pre-stage call, ≤10 lines of instance-specific tips, default off) | 2–3 days + 1–2 weeks | Phase 0 shows meaningful correction rates |
| **2** | Advice store + bandit-style selection; extend to efficiency + Q&A personalization | incremental | Phase 1 shows improvement over baseline |
| **3** | Train a small open-weight advisor model (GRPO) — the full paper vision | months + GPU | Volume + infrastructure ready |

Phase 3 is contingent on infrastructure and data volume that don't exist yet. Phases 0–1 are self-contained and low-risk.

---

## Why not just improve the playbook or skill prompts?

Better playbooks and prompt optimization (e.g. GEPA) find the best *static* rule for a domain. They can't vary per project or per reviewer. The advisor's differentiation is exactly the cases where the right guidance depends on *this* run's context — which no single fixed prompt can express.

---

## Honest counterargument

Phase 1 is not far from a well-maintained playbook that the skill already reads. The marginal improvement might be modest. Phase 0 baselines are the check — if Opus is already getting most mappings right first-pass, there's little headroom to gain.

---

## Decision needed

Run the Phase 0 measurement first. If first-pass correction rates are non-trivial in any domain, Phase 1 pays off. See [open-items.md](open-items.md) for the tracked decision.
