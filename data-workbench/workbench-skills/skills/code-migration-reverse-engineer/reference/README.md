# Source Platform SME corpus (reverse-engineering)

Curated, version-controlled reference content describing **what to identify** in
legacy code for a given source platform. The `code-migration-reverse-engineer`
skill reads only the subset matching the source platform (and version) named in
the stage's `CODE MIGRATION CONTEXT` directive.

## Layout

```
reference/
  <platform>/<version>/*.md   # version-specific, most specific wins
  <platform>/*.md             # platform-wide fallback
```

## Override contract (client tweaking)

This corpus is plain markdown under version control. A client who wants the
reverse-engineer step to recognize additional constructs (in-house UDFs, internal
libraries, naming conventions) **forks this directory and rebuilds the backend
image** — the vendored skill is baked in at build time. Each entry should name the
construct, its `kind`, why it matters for migration, and (optionally) the target
consideration. Keep entries factual and testable; the reviewer sees what you flag.

Platforms currently covered: `mysql`, `postgres`, `teradata`, `oracle`.
