---
name: migration-assessment-advisor
description: Assesses a discovered source for a data-migration (dmig) project and produces a migration plan. Reads discovered table metadata to report per-table keys, incremental-cursor candidates, and a recommended write mode, and flags lossy/ambiguous type conversions for the chosen target platform using the bundled per-platform corpus. Trigger for the dmig_assess_plan stage or when the user asks to "assess the source for migration", "plan the migration", or "check type conversions". Framework-neutral (about the source, not the mover).
---

# Migration Assessment Advisor

Assesses the discovered source for a **data migration** (`dmig`) project and emits
`assessment.json`. Framework-neutral — it describes the *source*, so it is the same
regardless of which mover (DLT, etc.) runs later.

## Steps

1. Read the migration directive in your prompt — it names the **target platform**
   and **landing strategy** (raw / lift-and-shift in this phase).
2. Run the assessment script (all structural logic is bundled — run it, do not
   re-implement):

   ```
   python ${CLAUDE_SKILL_DIR}/scripts/assess_source.py \
     --discovery-dir data_discovery \
     --target-platform <target_platform> \
     --output assessment.json
   ```

   It reports per table: column count, primary key, incremental-cursor candidates
   (monotonic-int PK, `updated_at`/timestamp columns), a recommended write mode,
   and FK count.
3. **Type-conversion review.** For the target platform, read
   `reference/platforms/<platform>.md` + `reference/platforms/<platform>.yaml` and
   call out any lossy / ambiguous / unsupported conversions for the discovered
   column types (e.g. Oracle `NUMBER` without precision, unsigned integers,
   timezone-naive timestamps). Do NOT restate a type→canonical table — that lives
   in the canonical type system (`platform.type_system`); the corpus is prose
   guidance that complements it.
4. Summarize the assessment in your response: table count, tables with no primary
   key (a merge/append risk), incremental candidates, and any flagged type
   conversions. This grounds the engineer's Configure Migration + generation steps.

## Reference corpus (bundled)

`reference/platforms/<platform>.{yaml,md}` — per-platform idioms + gotchas
(prose/idiom only; never a type-mapping table).
