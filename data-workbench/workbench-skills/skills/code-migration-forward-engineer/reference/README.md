# Target Platform SME corpus (forward-engineering)

Curated, version-controlled reference content describing the target platform's
**libraries, patterns, techniques, best practices, and anti-patterns**. The
`code-migration-forward-engineer` skill reads only the subset matching the target
platform named in the stage's `CODE MIGRATION CONTEXT` directive.

## Layout

```
reference/
  <platform>/*.md   # e.g. databricks/, snowflake/
```

## Override contract (client tweaking) — this is the steering point

This is the corpus a **client tweaks to control how converted code looks**: naming
conventions, preferred patterns, banned anti-patterns, house standards. It is plain
markdown under version control. A client forks `reference/<platform>/` and rebuilds
the backend image; the forward stage then applies their rules instead of the
defaults. Editing this corpus invalidates a project's conversion/package output (but
not the reviewed spec) — re-run forward-engineering to pick up the change.

Each file should be prescriptive: state the pattern to use, show a short example, and
list the anti-patterns to avoid. Platforms currently covered: `databricks`, `snowflake`.
