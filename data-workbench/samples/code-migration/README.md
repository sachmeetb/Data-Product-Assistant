# Code-migration samples

Realistic **legacy code** written against the Workbench sample databases, for
exercising the **Code Migration (`cmig`)** archetype end to end. Each artifact is
deliberately full of source-platform-specific idioms (the things the
`code-migration-reverse-engineer` skill's SME corpus is meant to catch) so the
reverse→review→forward flow has something to bite on.

Unlike the DB samples (`banking/`, `hr/`, `hr-mysql/`, `products_sales/`), these are
**not** loaded into a database. They are *code* you import into a `cmig` project via
**Import Legacy Code** (or the `import_code` MCP tool). A `cmig` project links to a
completed data-migration (`dmig`) project for the source→target schema, so the
natural pairing is:

| Sample | Source platform | Sample DB it queries | Suggested migration target |
|---|---|---|---|
| `mysql-hr/` | MySQL | `hr-mysql` (`hr_core`, `hr_comp`) | Databricks |
| `postgres-products/` | PostgreSQL | `products_sales` | Snowflake or Databricks |

## Layout

```
samples/code-migration/
  <sample>/
    code.json        # manifest: source platform + version + which sample DB + entry files
    <legacy files>   # the code to convert (single file per sample today; folder-ready)
    README.md        # what the code does + the constructs it exercises
```

`code.json` mirrors the DB samples' `sample.json` convention: it self-describes the
source platform and the sample database the code targets, so a demo script (or a
test) can pair the code with the right `dmig` project automatically.

## Using a sample (demo flow)

1. Run a `dmig` migration of the paired sample DB (e.g. `hr-mysql` → Databricks)
   through **Reconcile** so the target `:Dataset` nodes exist.
2. Create a **Code Migration** project; **Link Migration** to that `dmig` project.
3. **Import Legacy Code** — upload the files from the sample dir.
4. **Configure Conversion** (source platform/version from `code.json`, target artifact
   kind), **Reverse-Engineer Spec**, review + **Approve** the spec, then
   **Forward-Engineer Code**, and **Package**.
