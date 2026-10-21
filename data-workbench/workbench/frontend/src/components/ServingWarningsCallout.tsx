/**
 * Renders the structured warnings, auto-bridge picks, and SCD lowering state
 * emitted by `~/.claude/skills/data-serving-virtual-view/scripts/generate_view_ddl.py`.
 *
 * The skill persists its summary JSON on :ServingDefinition.summaryJson; the
 * backend (summary.py SERVING_QUERY, marketplace.py PRODUCT_DETAIL) returns
 * it as `summary_json` / `summaryJson` to the frontend. This component
 * parses it defensively (empty / malformed → render nothing) and groups
 * surfaces by view so a multi-output-dataset product shows per-view callouts.
 *
 * Why a structured callout vs just letting the engineer read `-- comments`
 * in the DDL: the DDL is collapsed behind "View DDL" by default and engineers
 * rarely scroll to the top of a 60-line generated view. Multiplication
 * warnings + SCD warnings are correctness signals — they need to be visible
 * BEFORE the DDL is expanded, not buried inside it.
 *
 * Hidden entirely when there are no warnings of any kind across all views
 * (the common case for a clean source-aligned product).
 */

import { useState } from "react";

interface MultiplicationWarning {
  table: string;
  temporal_column: string;
  suggestion: string;
}

interface AutoBridgeChoice {
  path: string[];
  rationale: string;
  bridge_key?: string;
  temporal_column?: string | null;
}

interface JoinDiagnostic {
  /** "join_predicate_warning" (explicit-join column not found — warn) or
   *  "validation_unavailable" (column metadata missing — couldn't verify). */
  kind: string;
  alias?: string;
  column: string;
  table?: string;
  uri?: string;
  existing_columns?: string[];
  reason?: string;
}

interface ViewSummary {
  view_name: string;
  multiplication_warnings?: MultiplicationWarning[];
  auto_bridge_choice?: AutoBridgeChoice[];
  scd_warning?: string | null;
  scd_synthesized_dedupe?: boolean;
  scd_policy_type?: string;
  underspecified_aggregates?: string[];
  /** Phase 5: product columns dropped from the final SELECT per
   *  :DatasetTransform.suppressedColumns. Still present in the contract /
   *  graph; only the served view omits them. */
  suppressed_columns?: string[];
  /** Declared suppressed names that didn't match any product column —
   *  almost always a typo. */
  suppressed_unknown?: string[];
  /** Suppressions the renderer refused: PK columns can't be suppressed
   *  without breaking the contract. */
  suppressed_pks_skipped?: string[];
  /** Phase 5 SCD-2 fields. Populated only when scd_policy_type='scd2' AND
   *  the column was actually validated against product columns. */
  scd_effective_column?: string;
  scd_expiration_column?: string;
  scd_is_current_derived?: boolean;
  /** Phase 5 snapshot-SCD fields. Populated only when scd_policy_type='snapshot'
   *  AND the column + date were validated; scd_synthesized_filter indicates
   *  whether the WHERE predicate was actually synthesized (false when
   *  validation failed — see scd_warning). */
  scd_snapshot_column?: string;
  scd_snapshot_as_of?: string;
  scd_synthesized_filter?: boolean;
  /** Phase 6 window functions. `window_specs_used` lists named windows that
   *  some mapping referenced; `window_specs_undefined` lists references that
   *  didn't resolve to a declared spec (typo / missing declaration — view
   *  fell back to degenerate OVER ()). */
  window_specs_used?: string[];
  window_specs_undefined?: string[];
  /** Build-time JOIN-column validation: explicit-join column-not-found warnings
   *  + validation_unavailable (metadata missing) notices. A proven-absent FK
   *  column hard-fails generation instead of landing here. */
  join_diagnostics?: JoinDiagnostic[];
}

interface TopSummary {
  views?: ViewSummary[];
}

function parseSummary(raw: string | null | undefined): TopSummary | null {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return null;
    return parsed as TopSummary;
  } catch {
    return null;
  }
}

function viewHasAnyWarning(v: ViewSummary): boolean {
  return (
    (v.multiplication_warnings && v.multiplication_warnings.length > 0) ||
    (v.auto_bridge_choice && v.auto_bridge_choice.length > 0) ||
    !!v.scd_warning ||
    !!v.scd_synthesized_dedupe ||
    !!v.scd_effective_column ||
    !!v.scd_expiration_column ||
    !!v.scd_synthesized_filter ||
    (v.underspecified_aggregates && v.underspecified_aggregates.length > 0) ||
    (v.suppressed_columns && v.suppressed_columns.length > 0) ||
    (v.suppressed_unknown && v.suppressed_unknown.length > 0) ||
    (v.suppressed_pks_skipped && v.suppressed_pks_skipped.length > 0) ||
    (v.window_specs_used && v.window_specs_used.length > 0) ||
    (v.window_specs_undefined && v.window_specs_undefined.length > 0) ||
    (v.join_diagnostics && v.join_diagnostics.length > 0) ||
    false
  );
}

interface Props {
  /** Raw JSON string from :ServingDefinition.summaryJson. Empty / invalid →
   *  callout is not rendered. */
  summaryJson: string | null | undefined;
  /** Default closed (compact mode). Set `true` for surfaces where vertical
   *  space is generous. */
  defaultOpen?: boolean;
}

export default function ServingWarningsCallout({ summaryJson, defaultOpen = false }: Props) {
  const [open, setOpen] = useState(defaultOpen);
  const summary = parseSummary(summaryJson);
  if (!summary || !summary.views) return null;
  const viewsWithWarnings = summary.views.filter(viewHasAnyWarning);
  if (viewsWithWarnings.length === 0) return null;

  const totalCount = viewsWithWarnings.reduce((sum, v) => {
    const scd2Active = !!(v.scd_effective_column || v.scd_expiration_column);
    return (
      sum +
      (v.multiplication_warnings?.length || 0) +
      (v.auto_bridge_choice?.length || 0) +
      (v.scd_warning ? 1 : 0) +
      (v.scd_synthesized_dedupe ? 1 : 0) +
      (scd2Active ? 1 : 0) +
      (v.scd_synthesized_filter ? 1 : 0) +
      (v.underspecified_aggregates?.length || 0) +
      (v.suppressed_columns?.length ? 1 : 0) +
      (v.suppressed_unknown?.length ? 1 : 0) +
      (v.suppressed_pks_skipped?.length ? 1 : 0) +
      (v.window_specs_used?.length ? 1 : 0) +
      (v.window_specs_undefined?.length || 0) +
      (v.join_diagnostics?.length || 0)
    );
  }, 0);

  return (
    <div style={{ border: "1px solid #fbbf24", borderRadius: 6, backgroundColor: "#fffbeb", marginTop: 6 }}>
      <button
        onClick={() => setOpen(!open)}
        style={{
          width: "100%", padding: "6px 10px", border: "none", background: "transparent",
          textAlign: "left", cursor: "pointer", display: "flex", alignItems: "center",
          gap: 8, fontSize: 12, fontWeight: 600, color: "#92400e",
        }}
        title="Surfaces from view-DDL generator (skill: data-serving-virtual-view)"
      >
        <span style={{ fontSize: 13 }}>{open ? "▾" : "▸"}</span>
        <span>View-DDL signals ({totalCount})</span>
      </button>
      {open && (
        <div style={{ padding: "4px 12px 10px 12px", fontSize: 12, color: "#451a03" }}>
          {viewsWithWarnings.map((v) => (
            <ViewWarningBlock key={v.view_name} view={v} multipleViews={viewsWithWarnings.length > 1} />
          ))}
        </div>
      )}
    </div>
  );
}

function ViewWarningBlock({ view, multipleViews }: { view: ViewSummary; multipleViews: boolean }) {
  return (
    <div style={{ marginTop: 6 }}>
      {multipleViews && (
        <div style={{ fontWeight: 600, marginBottom: 4, color: "#78350f" }}>
          {view.view_name}
        </div>
      )}
      {view.scd_synthesized_dedupe && (
        <Signal tone="info" label="SCD-1 lowering">
          scd_policy.type=<code>{view.scd_policy_type || "latest_only"}</code> lowered to a synthetic dedupe block — view emits one row per natural key, latest first.
        </Signal>
      )}
      {(view.scd_effective_column || view.scd_expiration_column) && (
        <Signal tone="info" label="SCD-2 history preserved">
          <div>
            scd_policy.type=<code>scd2</code> — history is preserved (no dedupe).
            Consumers interpret validity from{" "}
            {view.scd_effective_column && (
              <>effective: <code>{view.scd_effective_column}</code></>
            )}
            {view.scd_effective_column && view.scd_expiration_column && " / "}
            {view.scd_expiration_column && (
              <>expiration: <code>{view.scd_expiration_column}</code></>
            )}
            .
          </div>
          {view.scd_is_current_derived && (
            <div style={{ marginTop: 2, fontSize: 11, color: "#78350f" }}>
              View also surfaces a derived <code>is_current</code> boolean for easy current-row filtering.
            </div>
          )}
        </Signal>
      )}
      {view.scd_synthesized_filter && (
        <Signal tone="info" label="Snapshot SCD">
          <div>
            scd_policy.type=<code>snapshot</code> — view pinned to{" "}
            <code>{view.scd_snapshot_column}</code> ={" "}
            <code>'{view.scd_snapshot_as_of}'</code>. Synthesized WHERE filter
            (composes with any engineer-declared filter via AND).
          </div>
        </Signal>
      )}
      {view.scd_warning && (
        <Signal tone="warn" label="SCD warning">{view.scd_warning}</Signal>
      )}
      {(view.auto_bridge_choice || []).map((b, i) => (
        <Signal key={`b-${i}`} tone="info" label="Auto-bridge">
          <div>
            Path: <code>{(b.path || []).join(" → ")}</code>
          </div>
          <div style={{ marginTop: 2, fontSize: 11, color: "#78350f" }}>{b.rationale}</div>
          {b.temporal_column && (
            <div style={{ marginTop: 2, fontSize: 11, color: "#78350f" }}>
              Wrapped in latest-row semantics ordered by <code>{b.temporal_column}</code> DESC.
            </div>
          )}
        </Signal>
      ))}
      {(view.multiplication_warnings || []).map((w, i) => (
        <Signal key={`m-${i}`} tone="warn" label="Row multiplication">
          <div>
            <code>{w.table}</code> has temporal column <code>{w.temporal_column}</code> with no <code>:DatasetTransform.dedupe</code>.
          </div>
          <div style={{ marginTop: 2, fontSize: 11 }}>{w.suggestion}</div>
        </Signal>
      ))}
      {(view.underspecified_aggregates || []).length > 0 && (
        <Signal tone="warn" label="Aggregation under-specified">
          Grouped output: column{(view.underspecified_aggregates?.length || 0) > 1 ? "s" : ""}{" "}
          {(view.underspecified_aggregates || []).map((c) => <code key={c}>{c}</code>).reduce<JSX.Element[]>((acc, el, i) => {
            if (i > 0) acc.push(<span key={`s-${i}`}>, </span>);
            acc.push(el);
            return acc;
          }, [])}{" "}
          fell back to <code>MAX()</code>. Set an explicit aggregateFunction in the TransformEditor.
        </Signal>
      )}
      {(view.suppressed_columns || []).length > 0 && (
        <Signal tone="info" label="Columns suppressed">
          <div>
            Per <code>:DatasetTransform.suppressedColumns</code>, the following product
            column{(view.suppressed_columns?.length || 0) > 1 ? "s are" : " is"} omitted from the
            served view (still in the contract for lineage):{" "}
            {(view.suppressed_columns || []).map((c, i, arr) => (
              <span key={c}>
                <code>{c}</code>{i < arr.length - 1 ? ", " : ""}
              </span>
            ))}
            .
          </div>
        </Signal>
      )}
      {(view.suppressed_pks_skipped || []).length > 0 && (
        <Signal tone="warn" label="PK suppression refused">
          Primary-key column{(view.suppressed_pks_skipped?.length || 0) > 1 ? "s" : ""}{" "}
          {(view.suppressed_pks_skipped || []).map((c, i, arr) => (
            <span key={c}><code>{c}</code>{i < arr.length - 1 ? ", " : ""}</span>
          ))}{" "}
          can't be suppressed without breaking the contract — kept in SELECT.
        </Signal>
      )}
      {(view.suppressed_unknown || []).length > 0 && (
        <Signal tone="warn" label="Unknown in suppressedColumns">
          <code>:DatasetTransform.suppressedColumns</code> referenced product column{(view.suppressed_unknown?.length || 0) > 1 ? "s" : ""}{" "}
          {(view.suppressed_unknown || []).map((c, i, arr) => (
            <span key={c}><code>{c}</code>{i < arr.length - 1 ? ", " : ""}</span>
          ))}{" "}
          that don't exist on this dataset (likely typo).
        </Signal>
      )}
      {(view.window_specs_used || []).length > 0 && (
        <Signal tone="info" label="Window functions">
          <div>
            View uses {(view.window_specs_used?.length || 0) > 1 ? "named windows" : "named window"}:{" "}
            {(view.window_specs_used || []).map((c, i, arr) => (
              <span key={c}><code>{c}</code>{i < arr.length - 1 ? ", " : ""}</span>
            ))}
            . SQL OVER clauses are inlined from <code>:DatasetTransform.window_specs</code>; the audit comment at the top of the view names each.
          </div>
        </Signal>
      )}
      {(view.window_specs_undefined || []).map((nm, i) => (
        <Signal key={`wu-${i}`} tone="warn" label="Undefined window">
          A mapping references window <code>{nm}</code>, but no declaration exists in <code>:DatasetTransform.window_specs</code>. SQL fell back to degenerate <code>OVER ()</code> — almost certainly not the intended semantics.
        </Signal>
      ))}
      {(view.join_diagnostics || []).map((d, i) =>
        d.kind === "validation_unavailable" ? (
          <Signal key={`jd-${i}`} tone="info" label="Join not verified">
            Couldn't verify join column <code>{d.alias ? `${d.alias}.` : ""}{d.column}</code>
            {d.uri ? <> on <code>{d.uri}</code></> : null} — column metadata was unavailable, so
            existence wasn't checked at build.
          </Signal>
        ) : (
          <Signal key={`jd-${i}`} tone="warn" label="Join column not found">
            <div>
              Explicit join predicate references <code>{d.alias ? `${d.alias}.` : ""}{d.column}</code>,
              but <code>{d.table}</code> has no such column. If it's a typo the view will fail at
              deploy; if <code>{d.column}</code> is a computed/aliased expression this is benign.
            </div>
            {d.existing_columns && d.existing_columns.length > 0 && (
              <div style={{ marginTop: 2, fontSize: 11, color: "#78350f" }}>
                Columns that exist: {d.existing_columns.map((c, j, arr) => (
                  <span key={c}><code>{c}</code>{j < arr.length - 1 ? ", " : ""}</span>
                ))}
              </div>
            )}
          </Signal>
        )
      )}
    </div>
  );
}

function Signal({ tone, label, children }: { tone: "info" | "warn"; label: string; children: React.ReactNode }) {
  const palette = tone === "warn"
    ? { bg: "#fef3c7", border: "#fbbf24", chip: "#b45309" }
    : { bg: "#dbeafe", border: "#60a5fa", chip: "#1e40af" };
  return (
    <div
      style={{
        display: "flex", gap: 8, alignItems: "flex-start",
        padding: "6px 8px", marginTop: 4,
        backgroundColor: palette.bg, borderLeft: `3px solid ${palette.border}`,
        borderRadius: 4,
      }}
    >
      <span
        style={{
          fontSize: 10, fontWeight: 700, padding: "1px 6px", borderRadius: 3,
          backgroundColor: "#fff", color: palette.chip, whiteSpace: "nowrap",
          letterSpacing: 0.2, textTransform: "uppercase",
        }}
      >
        {label}
      </span>
      <div style={{ flex: 1, lineHeight: 1.4 }}>{children}</div>
    </div>
  );
}
