---
name: code-migration-package-documenter
description: Writes a contextual README for a code-migration (cmig) downloadable package. Reads the assembled package (old/, new/, codespec.json, conversion.json) and returns a JSON {files} verdict with a README.md that explains what was converted, the original→spec→design→new-code lineage, and the conversion outcome (converted / manual_action / unsupported). Programmatic-only — invoked by the backend when assembling the package; a deterministic Python fallback (serving_package._fallback_code_migration_readme) covers the same seven-section structure when this skill isn't used.
---

# Code Migration — Package Documenter

Produce a clear README for a code-migration package so a reviewer can browse and hand
it off. The backend calls this optionally; a deterministic fallback covers the same
structure, so this skill only needs to make the prose richer and more specific.

## Steps

1. Read the assembled package: `old/` (original code), `new/` (converted code),
   `codespec.json`, `conversion.json`.
2. Return a JSON verdict of the form:

   ```json
   {"files": {"README.md": "<full markdown>"}}
   ```

3. The README must follow this section order:
   - **Title + what this is** — the `<source> → <target>` conversion, artifact kind.
   - **Artifact inventory** — a table of `old/`, `new/`, `codespec.json`, `design.md`,
     `conversion.json`.
   - **What was converted** — the intent from `codespec.json`, the file list.
   - **Conversion outcome** — counts of `converted` / `manual_action` / `unsupported`
     from `conversion.json`; call out anything needing a human (this is a
     *converted-with-actions* result, not a clean success).
   - **Lineage** — original → spec → design → new code.
   - **How to review it** — read the spec, diff old/ vs new/, check conversion.json.

Keep it factual and specific to *this* conversion; never claim complete equivalence
when `conversion.json` has manual_action/unsupported items.
