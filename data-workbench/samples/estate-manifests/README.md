# Sample estate manifests (offline extraction)

Prebuilt, reviewable `estate-manifest-*.yaml` documents produced by the offline
metadata **extraction kit** (`workbench/backend/extraction_runners/`). They let you
demo — or smoke-test — the **offline import** path end-to-end **with no live database
and no scan cost**: add an *offline* estate source, then import one of these files.

| File | Source | Shape |
|---|---|---|
| `greenfield-orders-sample.yaml` | hand-written, small + readable | 2 tables (`orders`→`customers`), PK/FK, a PII-redacted column — **the easiest one to eyeball** |
| `hr.yaml` | the compose `hr` Postgres DB | 2 schemas (`hr_core`, `hr_comp`), 11 tables, FKs, guarded profiles |
| `products_sales.yaml` | the compose `products_sales` DB | sales/catalog tables, self-FK category tree, order history |

The **same file works for both paths** — the estate import (replayed into an `:EstateScan`)
and the greenfield source-aligned import (seeded into a `dpe-sa` project's catalog +
profiling). The platform is just a field inside.

Each was generated deterministically with:

```bash
WB_SOURCE_PLATFORM=postgres \
WB_SOURCE_DSN="postgresql://user:pw@host:5432/<db>" \
python workbench/backend/extraction_runners/run_extract.py extract --all
```

They are **DCAT-in-YAML**: one platform-agnostic document per source that serializes
exactly what a live scan captures (schemas / tables / columns / types / keys / FKs /
volumetrics) plus guarded, PII-redacted profiling. Import replays them into the same
graph writer a live scan uses, so an imported scan is indistinguishable from a live one.

**To import in the UI:** Product Workbench → **Connected Estate** → add a source with
**"Can't connect — offline"** (platform `postgres`, catalog e.g. `hr`) → **Import scan**
→ pick the file → review the preview → **Confirm import**. Then **Enrich** and run
**Feasibility** exactly as you would for a live scan.

See [`docs/offline-extraction.md`](../../docs/offline-extraction.md) for the full guide.
