# Open Items

Ongoing decisions and gaps that need enacting. Remove an item once it ships; add new ones here rather than burying them in research docs.

---

## Security

### Role-gating on `/po-mcp` — highest priority before any remote/multi-tenant deploy
The `/mcp` vs `/po-mcp` split is surface-area separation only. Any valid `WB_MCP_TOKENS` bearer can self-declare `role="Data Product Owner"` and reach PO-privileged actions (`deploy_product`, `bulk_approve_source_validation`, `resolve_pushback`).

**Fix:** extend the token grammar to bind roles (`token:principal:projects:roles`) and enforce in `_role_guard` (`mcp_server.py`). Parity: same guard needed in `po_mcp_server.py`. Add tests.

**Effort:** M. **Gate:** must be decided before any remote or multi-tenant deployment.

---

## Integrations

### OpenLineage producer emitter
Emit OpenLineage `RunEvent`s with `columnLineage` facets at `deploy_virtual_view` and dbt build completion. One emitter makes every deployed product visible — with column-level lineage — in DataHub, Fabric, DataZone, OpenMetadata, Manta/watsonx, and others simultaneously.

**Approach:** `lineage_emitter.py` translating `(product, approved ColumnMappings, LOOKUP_VIA edges)` → OL events; HTTP transport config per project in `AppSettings`; fired from the two deploy paths. Marquez docker-compose as local test harness.

**Effort:** bounded (self-contained new module). **Source:** `research/2026-07-10-edge-platform-landscape.md` §2.1.

---

## Architecture

### Core-vs-edge ontology boundary — four open questions (for discussion with Scott)
The one-pager (`research/2026-07-10-core-vs-edge-onepager.md`) has four agreed positions to ratify and one open question per subdomain:

1. Does the prescriptive/descriptive split (owned-extension tier) address Scott's lineage/quality concerns?
2. OpenLineage **producer-first** before any lineage ingestion — agree with that ordering?
3. Is pointer-only `ruleSource='external'` the DQV-policy shape Scott and Neda had in mind for the quality edge?
4. Adopt the five-question modeling gate into the v3 ontology conventions?

**Effort:** discussion + doc update. **Source:** `research/2026-07-10-core-vs-edge-onepager.md`.

---

## Advisor models for `data_mapping`
Research (`research/2026-07-13-advisor-models-data-workbench.md`) established that DW already holds ~80% of the plumbing needed for per-instance advice injection (reward signals, PROV-O provenance, token ledger). Decision point: start the staged build?

**Proposed Phase 1 (~2–3 days + 1–2 weeks):**
- Add `{advice}` placeholder to `pipeline.py:build_prompt()` with a provider interface.
- Ship a `mapping-advisor` skill (one-shot pre-stage call, reads domain review history → ≤10 lines of instance-specific advice). Default off via `config_fields`.
- Persist advice→outcome pairs (`StageAdvice` table) from day one for later measurement.

**Effort:** S1 ~2–3 days, S2 ~1–2 weeks. **Gate:** decide whether to start Phase 1.

---

## Verification

### Cursor onboarding — one manual launch needed
The installers write `.cursor/mcp.json` + `.cursor/rules/*.mdc` in the documented format but no Cursor client has been launched to confirm it renders correctly.

**Effort:** XS (one manual test).
