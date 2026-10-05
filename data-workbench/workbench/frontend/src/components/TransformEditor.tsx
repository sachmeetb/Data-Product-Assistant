import { useEffect, useMemo, useState } from "react";
import {
  type TransformKind,
  type TransformAuthor,
  type MappingSource,
  type LookupSelectionStrategy,
  TRANSFORM_KIND_LABELS,
  TRANSFORM_AUTHOR_LABELS,
  STANDARDIZATION_OPTIONS,
  LOOKUP_STRATEGY_LABELS,
  LOOKUP_AGGREGATE_FUNCTIONS,
  COLUMN_AGGREGATE_FUNCTIONS,
} from "../types";

export interface TransformEditorValue {
  transform_kind: TransformKind;
  transform_expression: string;
  transform_inputs: string[];      // source column URIs
  transform_params: Record<string, unknown>;
  transform_decorators: { standardization?: string[]; default_if_null?: string };
  // Phase 3 of implementingdatatransformations.md — column-level aggregation.
  // Both no-ops at view-DDL render time unless the product :DatasetTransform
  // has grouping_keys set.
  aggregate_function?: string;
  grouping_key?: boolean;
}

interface Props {
  value: TransformEditorValue;
  sources: MappingSource[];
  author: TransformAuthor | null;
  confidence: number | null;
  /** Called on every committed change. Parent persists via /reviews/mappings edit_transform. */
  onChange: (next: TransformEditorValue) => void;
  /** Disabled while a save round-trip is in flight. */
  saving?: boolean;
  /** Hide raw-SQL toggle and the expression text area (used by the PO wizard which authors hints, not SQL). */
  mode?: "expression" | "hint";
  /** All source columns the project knows about — used to back column /
   *  table pickers for the lookup arm (lookup_table dropdown + key /
   *  value / order-by column pickers filtered to the chosen table).
   *  When omitted, the lookup fields fall back to freeform text inputs. */
  availableSources?: import("../types").SourceColumn[];
}

const KIND_OPTIONS: TransformKind[] = [
  "direct", "cast", "format", "concat", "split", "substring",
  "case", "arithmetic", "lookup", "literal", "expression",
  "bucket", "mask", "hash", "window", "date_difference",
];

// Phase 7 date_difference: the semantics discriminator is load-bearing — a
// syntactically-valid translation can silently change the business answer
// (completed elapsed years vs calendar-year boundaries crossed). Mirrors the
// capability artifact's date_difference op + generate_view_ddl.Dialect.
const DATE_DIFF_SEMANTICS: { value: string; label: string }[] = [
  { value: "completed_units", label: "Completed years (age) — full elapsed years" },
  { value: "boundary_count", label: "Calendar-year boundaries crossed (YEAR(end) − YEAR(start))" },
  { value: "symbolic_interval", label: "Symbolic interval yrs+mos+days (Postgres only)" },
];
const DATE_DIFF_SEMANTICS_DEFAULT = "completed_units";

// Window functions accepted by _compile_window in generate_view_ddl.py.
// Mirrored here so the dropdown stays in sync with what view-DDL will accept.
// Ranking functions take no argument; aggregates / LAG / LEAD / FIRST_VALUE
// take one (the source column the engineer wires via Inputs).
const WINDOW_FUNCTIONS: { value: string; label: string; takesArg: boolean }[] = [
  { value: "ROW_NUMBER",    label: "ROW_NUMBER() — sequential ordinal",                takesArg: false },
  { value: "RANK",          label: "RANK() — gapped ranking",                          takesArg: false },
  { value: "DENSE_RANK",    label: "DENSE_RANK() — gapless ranking",                   takesArg: false },
  { value: "NTILE",         label: "NTILE(buckets) — equal-size groups",               takesArg: false },
  { value: "PERCENT_RANK",  label: "PERCENT_RANK() — relative ranking [0,1]",          takesArg: false },
  { value: "CUME_DIST",     label: "CUME_DIST() — cumulative distribution [0,1]",      takesArg: false },
  { value: "LAG",           label: "LAG(col, offset, default) — previous row's value", takesArg: true  },
  { value: "LEAD",          label: "LEAD(col, offset, default) — next row's value",    takesArg: true  },
  { value: "FIRST_VALUE",   label: "FIRST_VALUE(col) — first row in window",           takesArg: true  },
  { value: "LAST_VALUE",    label: "LAST_VALUE(col) — last row in window",             takesArg: true  },
  { value: "SUM",           label: "SUM(col) — running sum",                           takesArg: true  },
  { value: "AVG",           label: "AVG(col) — running average",                       takesArg: true  },
  { value: "MIN",           label: "MIN(col) — running min",                           takesArg: true  },
  { value: "MAX",           label: "MAX(col) — running max",                           takesArg: true  },
  { value: "COUNT",         label: "COUNT(col) — running count",                       takesArg: true  },
  { value: "COUNT_DISTINCT",label: "COUNT(DISTINCT col) — running distinct count",     takesArg: true  },
];
const WINDOW_FUNCTION_DEFAULT = "ROW_NUMBER";
const WINDOW_FN_TAKES_ARG: Record<string, boolean> = Object.fromEntries(
  WINDOW_FUNCTIONS.map((f) => [f.value, f.takesArg])
);

// Mirror of _VALID_AGG_FUNCTIONS in generate_view_ddl.py — kept here so the
// preview can render the same SQL the renderer will emit.
const _AGG_FN_DEFAULT: typeof LOOKUP_AGGREGATE_FUNCTIONS[number] = "SUM";

/**
 * Build a SQL-ish preview from the structured fields. This mirrors the
 * compilation logic in generate_view_ddl.py at a single-mapping granularity,
 * so what the engineer sees here is what the view DDL will emit.
 *
 * Substitution uses bare column names (last segment of each source URI).
 * For lookup, we render `<lookup_table>.<value_column>` — the actual JOIN
 * is added by the DDL generator at view-build time.
 */
// Pure helper shared with TransformEditorDialog's compact preview. Not a component;
// the react-refresh rule only matters for dev HMR, and this stays a stable pure fn.
// eslint-disable-next-line react-refresh/only-export-components
export function buildPreview(value: TransformEditorValue, sources: MappingSource[]): string {
  const { transform_kind, transform_expression, transform_inputs, transform_params, transform_decorators } = value;
  const params = transform_params || {};
  const decorators = transform_decorators || {};

  // Map source URI -> bare name. Fall back to the source list when an input
  // has no entry there yet.
  const bareName = (uri: string): string => {
    const found = sources.find((s) => s.uri === uri);
    if (found?.name) return found.name;
    const m = uri.match(/[^.]+$/);
    return m ? m[0] : uri;
  };

  let expr = "";
  switch (transform_kind) {
    case "direct": {
      const first = transform_inputs[0] ? bareName(transform_inputs[0]) : (sources[0]?.name ?? "<col>");
      expr = first;
      break;
    }
    case "cast": {
      const first = transform_inputs[0] ? bareName(transform_inputs[0]) : (sources[0]?.name ?? "<col>");
      const target = (params.target_type as string) || "TEXT";
      expr = transform_expression || `CAST(${first} AS ${target})`;
      break;
    }
    case "format": {
      const first = transform_inputs[0] ? bareName(transform_inputs[0]) : (sources[0]?.name ?? "<col>");
      const fmt = ((params.case as string) || "").toLowerCase();
      if (fmt === "upper") expr = `UPPER(${first})`;
      else if (fmt === "lower") expr = `LOWER(${first})`;
      else if (params.format) expr = `TO_CHAR(${first}, '${params.format}')`;
      else expr = transform_expression || first;
      break;
    }
    case "concat": {
      const sep = (params.separator as string) ?? " ";
      const parts = transform_inputs.length
        ? transform_inputs.map(bareName)
        : sources.map((s) => s.name || "<col>");
      expr = parts.join(` || '${sep}' || `);
      break;
    }
    case "split": {
      const first = transform_inputs[0] ? bareName(transform_inputs[0]) : (sources[0]?.name ?? "<col>");
      const delim = (params.delimiter as string) ?? " ";
      const idx = (params.index as number) ?? 1;
      expr = `SPLIT_PART(${first}, '${delim}', ${idx})`;
      break;
    }
    case "substring": {
      const first = transform_inputs[0] ? bareName(transform_inputs[0]) : (sources[0]?.name ?? "<col>");
      const start = (params.start as number) ?? 1;
      const len = (params.length as number) ?? 10;
      expr = `SUBSTRING(${first} FROM ${start} FOR ${len})`;
      break;
    }
    case "lookup": {
      const lookupTable = (params.lookup_table as string) || "<lookup_table>";
      const keyCol = (params.key_column as string) || "<key_col>";
      const valueCol = (params.value_column as string) || "<value_col>";
      const strat = ((params.selection_strategy as string) || "equi").toLowerCase();
      if (strat === "exists") {
        expr = `(lk.${keyCol} IS NOT NULL)`;
      } else if (strat === "latest") {
        const orderBy = (params.order_by_column as string) || "<order_by>";
        const dir = ((params.order_by_direction as string) || "DESC").toUpperCase();
        expr = `lk.${valueCol}  -- via ROW_NUMBER() OVER (PARTITION BY ${keyCol} ORDER BY ${orderBy} ${dir})`;
      } else if (strat === "aggregate") {
        const rawAgg = ((params.aggregate_expression as string) || "").trim();
        if (rawAgg) {
          expr = `lk.agg_value  -- ${rawAgg} GROUP BY ${keyCol} on ${lookupTable}`;
        } else {
          const fn = ((params.aggregate_function as string) || _AGG_FN_DEFAULT).toUpperCase();
          const aggExpr = fn === "COUNT_DISTINCT" ? `COUNT(DISTINCT ${valueCol})` : `${fn}(${valueCol})`;
          expr = `lk.${valueCol}  -- ${aggExpr} GROUP BY ${keyCol} on ${lookupTable}`;
        }
      } else {
        expr = `${lookupTable}.${valueCol}`;
      }
      break;
    }
    case "bucket": {
      const first = transform_inputs[0] ? bareName(transform_inputs[0]) : (sources[0]?.name ?? "<col>");
      const boundaries: number[] = (params.boundaries as number[]) || [];
      const labels: string[] = (params.labels as string[]) || [];
      if (!boundaries.length || labels.length !== boundaries.length + 1) {
        expr = `CASE ... END  -- bucket: ${boundaries.length} boundaries, ${labels.length} labels (need N+1)`;
      } else {
        const whens = boundaries.map((b, i) => `WHEN ${first} < ${b} THEN '${labels[i]}'`).join(" ");
        expr = `CASE ${whens} ELSE '${labels[labels.length - 1]}' END`;
      }
      break;
    }
    case "mask": {
      const first = transform_inputs[0] ? bareName(transform_inputs[0]) : (sources[0]?.name ?? "<col>");
      const algo = ((params.algorithm as string) || "keep_last").toLowerCase();
      const keepN = (params.keep_n as number) ?? 4;
      const mc = (params.mask_char as string) || "X";
      const fmt = Boolean(params.keep_format);
      if (algo === "keep_last") {
        expr = fmt
          ? `regexp_replace(left(${first}, length(${first}) - ${keepN}), '[A-Za-z0-9]', '${mc}', 'g') || right(${first}, ${keepN})`
          : `repeat('${mc}', length(${first}) - ${keepN}) || right(${first}, ${keepN})`;
      } else if (algo === "keep_first") {
        expr = fmt
          ? `left(${first}, ${keepN}) || regexp_replace(substring(${first}, ${keepN + 1}), '[A-Za-z0-9]', '${mc}', 'g')`
          : `left(${first}, ${keepN}) || repeat('${mc}', length(${first}) - ${keepN})`;
      } else {
        expr = `left(${first}, ${keepN}) || ... || right(${first}, ${keepN})  -- middle masked`;
      }
      break;
    }
    case "hash": {
      const first = transform_inputs[0] ? bareName(transform_inputs[0]) : (sources[0]?.name ?? "<col>");
      const algo = ((params.algorithm as string) || "md5").toLowerCase();
      const salt = (params.salt as string) || "";
      const target = salt ? `('${salt}' || CAST(${first} AS TEXT))` : `CAST(${first} AS TEXT)`;
      expr = algo === "md5"
        ? `md5(${target})`
        : `encode(digest(${target}, '${algo}'), 'hex')  -- requires pgcrypto`;
      break;
    }
    case "literal": {
      // The engineer types the SQL literal verbatim (with their own quotes
      // for strings, e.g. 'USD'). The optional cast appends ::<type>.
      const value = (params.literal_value as string) || "";
      const target = (params.target_type as string) || "";
      const base = value || "<literal>";
      expr = target ? `${base}::${target}` : base;
      break;
    }
    case "window": {
      // Phase 6 — window function. Mirrors _compile_window's argument shape
      // so the preview matches what view-DDL will emit. The actual OVER ()
      // clause is materialized at view build time from the named window
      // declared on :DatasetTransform.window_specs; we show a placeholder
      // referencing the name so the engineer can verify it's the right one.
      const fn = (((params.function as string) || WINDOW_FUNCTION_DEFAULT)).toUpperCase();
      const winName = (params.window as string) || "<window-name>";
      const takesArg = WINDOW_FN_TAKES_ARG[fn] ?? true;
      const arg = transform_inputs[0]
        ? bareName(transform_inputs[0])
        : (sources[0]?.name ?? "<col>");
      let call: string;
      if (fn === "COUNT_DISTINCT") {
        call = `COUNT(DISTINCT ${arg})`;
      } else if (fn === "LAG" || fn === "LEAD") {
        const offset = (params.offset as number) ?? 1;
        const dflt = params.default;
        const dfltSql = dflt == null
          ? ""
          : (typeof dflt === "string"
              ? `, '${String(dflt).replace(/'/g, "''")}'`
              : `, ${String(dflt)}`);
        call = `${fn}(${arg}, ${offset}${dfltSql})`;
      } else if (fn === "NTILE") {
        const n = (params.ntile as number) ?? 4;
        call = `NTILE(${n})`;
      } else if (!takesArg) {
        call = `${fn}()`;
      } else {
        call = `${fn}(${arg})`;
      }
      expr = `${call} OVER (${winName})`;
      break;
    }
    case "date_difference": {
      // Phase 7 neutral op. The concrete SQL is resolved per served platform at
      // view-DDL time (AGE on Postgres, TIMESTAMPDIFF on MySQL, …); the preview
      // shows the neutral intent so the semantics choice is unambiguous. inputs
      // are [start, end]; a single input pairs with CURRENT_DATE (age-from-dob).
      const semantics = (params.semantics as string) || DATE_DIFF_SEMANTICS_DEFAULT;
      const start = transform_inputs[0] ? bareName(transform_inputs[0]) : (sources[0]?.name ?? "<start>");
      const end = transform_inputs[1] ? bareName(transform_inputs[1]) : "CURRENT_DATE";
      expr = `date_difference(${start} → ${end}, unit=year, ${semantics})`;
      break;
    }
    case "case":
    case "arithmetic":
    case "expression":
    default:
      // These rely on the engineer's free-form expression. Fall back to the
      // first source if blank.
      expr = transform_expression || (transform_inputs[0] ? bareName(transform_inputs[0]) : (sources[0]?.name ?? ""));
      break;
  }

  // Decorators wrap whatever we built so far.
  const std = decorators.standardization || [];
  for (const op of std) {
    if (op === "trim") expr = `TRIM(${expr})`;
    else if (op === "upper") expr = `UPPER(${expr})`;
    else if (op === "lower") expr = `LOWER(${expr})`;
    else if (op === "normalize_whitespace") expr = `REGEXP_REPLACE(${expr}, '\\s+', ' ', 'g')`;
  }
  if (decorators.default_if_null != null && decorators.default_if_null !== "") {
    const v = decorators.default_if_null;
    const isNumeric = !Number.isNaN(Number(v)) && /^-?\d/.test(v);
    expr = `COALESCE(${expr}, ${isNumeric ? v : `'${String(v).replace(/'/g, "''")}'`})`;
  }
  return expr;
}

export default function TransformEditor({
  value, sources, author, confidence, onChange, saving = false, mode = "expression",
  availableSources,
}: Props) {
  const [rawMode, setRawMode] = useState(value.transform_kind === "expression");
  const [draftExpression, setDraftExpression] = useState(value.transform_expression);

  // Sync local raw-SQL draft when the parent swaps in a different mapping.
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { setDraftExpression(value.transform_expression); }, [value.transform_expression]);

  const preview = useMemo(() => buildPreview(value, sources), [value, sources]);
  const params = value.transform_params || {};
  const decorators = value.transform_decorators || {};
  const standardization: string[] = (decorators.standardization as string[] | undefined) || [];

  // Derive a table-keyed catalog from availableSources for the lookup arm
  // pickers. Each table appears once with its qualified `<schema>.<table>`
  // name and its column list. When availableSources is omitted (callers
  // that haven't been updated yet), the pickers fall back to freeform text.
  const tableCatalog = useMemo(() => {
    if (!availableSources || availableSources.length === 0) return null;
    const byTable = new Map<string, { qualified: string; columns: Array<{ name: string; data_type: string | null }> }>();
    for (const sc of availableSources) {
      const schema = sc.table_schema;
      const table = sc.table_name;
      const colName = sc.column_name;
      if (!schema || !table || !colName) continue;
      const qualified = `${schema}.${table}`;
      const entry = byTable.get(qualified) ?? { qualified, columns: [] };
      // Dedup columns by name (the UNION query can return the same column
      // twice if the same name appears across catalog + dprod paths).
      if (!entry.columns.some((c) => c.name === colName)) {
        entry.columns.push({ name: colName, data_type: sc.data_type });
      }
      byTable.set(qualified, entry);
    }
    return Array.from(byTable.values()).sort((a, b) => a.qualified.localeCompare(b.qualified));
  }, [availableSources]);

  // Columns scoped to the currently-typed lookup_table (so the key/value
  // pickers narrow to that table). Falls back to the union of all columns
  // when the engineer hasn't picked a table yet or picked one we haven't
  // discovered.
  const lookupTableColumns = useMemo(() => {
    if (!tableCatalog) return null;
    const lookupTable = (params.lookup_table as string) || "";
    const exact = tableCatalog.find((t) => t.qualified === lookupTable);
    if (exact) return exact.columns;
    return tableCatalog.flatMap((t) => t.columns);
  }, [tableCatalog, params.lookup_table]);

  const update = (patch: Partial<TransformEditorValue>) => onChange({ ...value, ...patch });
  const updateParams = (patch: Record<string, unknown>) => update({ transform_params: { ...params, ...patch } });
  const setStandardization = (op: string, on: boolean) => {
    const next = on ? Array.from(new Set([...standardization, op])) : standardization.filter((x) => x !== op);
    update({ transform_decorators: { ...decorators, standardization: next } });
  };
  const setDefaultIfNull = (v: string) => {
    update({
      transform_decorators: {
        ...decorators,
        default_if_null: v === "" ? undefined : v,
      },
    });
  };

  const toggleInput = (uri: string) => {
    const present = value.transform_inputs.includes(uri);
    const next = present
      ? value.transform_inputs.filter((u) => u !== uri)
      : [...value.transform_inputs, uri];
    update({ transform_inputs: next });
  };

  // Kind-specific param editor rows.
  const renderParamFields = () => {
    switch (value.transform_kind) {
      case "concat":
        return (
          <Field label="Separator">
            <input
              value={(params.separator as string) ?? " "}
              onChange={(e) => updateParams({ separator: e.target.value })}
              style={inputStyle}
              placeholder="' '"
            />
          </Field>
        );
      case "cast":
        return (
          <Field label="Target SQL type">
            <input
              value={(params.target_type as string) ?? ""}
              onChange={(e) => updateParams({ target_type: e.target.value })}
              style={inputStyle}
              placeholder="VARCHAR(50), DATE, NUMERIC(10,2), ..."
            />
          </Field>
        );
      case "format":
        return (
          <>
            <Field label="Case">
              <select
                value={(params.case as string) ?? ""}
                onChange={(e) => updateParams({ case: e.target.value })}
                style={inputStyle}
              >
                <option value="">(none)</option>
                <option value="upper">UPPER</option>
                <option value="lower">lower</option>
              </select>
            </Field>
            <Field label="Date format pattern (optional)">
              <input
                value={(params.format as string) ?? ""}
                onChange={(e) => updateParams({ format: e.target.value })}
                style={inputStyle}
                placeholder="YYYY-MM-DD"
              />
            </Field>
          </>
        );
      case "split":
        return (
          <>
            <Field label="Delimiter">
              <input
                value={(params.delimiter as string) ?? " "}
                onChange={(e) => updateParams({ delimiter: e.target.value })}
                style={inputStyle}
              />
            </Field>
            <Field label="Index (1-based)">
              <input
                type="number" min={1}
                value={(params.index as number) ?? 1}
                onChange={(e) => updateParams({ index: Number(e.target.value) })}
                style={inputStyle}
              />
            </Field>
          </>
        );
      case "substring":
        return (
          <>
            <Field label="Start (1-based)">
              <input
                type="number" min={1}
                value={(params.start as number) ?? 1}
                onChange={(e) => updateParams({ start: Number(e.target.value) })}
                style={inputStyle}
              />
            </Field>
            <Field label="Length">
              <input
                type="number" min={1}
                value={(params.length as number) ?? 10}
                onChange={(e) => updateParams({ length: Number(e.target.value) })}
                style={inputStyle}
              />
            </Field>
          </>
        );
      case "lookup": {
        const strategy: LookupSelectionStrategy =
          ((params.selection_strategy as LookupSelectionStrategy) ?? "equi");
        const aggExpr = ((params.aggregate_expression as string) ?? "").trim();
        return (
          <>
            <Field label="Strategy">
              <select
                value={strategy}
                onChange={(e) => updateParams({ selection_strategy: e.target.value })}
                style={{ ...inputStyle, width: 220 }}
              >
                {(Object.keys(LOOKUP_STRATEGY_LABELS) as LookupSelectionStrategy[]).map((s) => (
                  <option key={s} value={s}>{LOOKUP_STRATEGY_LABELS[s]}</option>
                ))}
              </select>
            </Field>
            <Field label="Lookup table (schema.table)">
              <input
                list={tableCatalog ? "tx-lookup-tables" : undefined}
                value={(params.lookup_table as string) ?? ""}
                onChange={(e) => updateParams({ lookup_table: e.target.value })}
                style={inputStyle}
                placeholder={tableCatalog ? "start typing to pick from discovered tables…" : "ref.iso_country"}
              />
              {tableCatalog && (
                <datalist id="tx-lookup-tables">
                  {tableCatalog.map((t) => (
                    <option key={t.qualified} value={t.qualified} />
                  ))}
                </datalist>
              )}
            </Field>
            <Field label="Key column (FK side)">
              <input
                list={lookupTableColumns ? "tx-lookup-columns" : undefined}
                value={(params.key_column as string) ?? ""}
                onChange={(e) => updateParams({ key_column: e.target.value })}
                style={inputStyle}
                placeholder={lookupTableColumns ? "pick from the chosen table's columns" : "code"}
              />
              {lookupTableColumns && (
                <datalist id="tx-lookup-columns">
                  {lookupTableColumns.map((c) => (
                    <option key={c.name} value={c.name}>
                      {c.data_type ? `(${c.data_type})` : ""}
                    </option>
                  ))}
                </datalist>
              )}
            </Field>
            {strategy !== "exists" && (
              <Field label={`Value column (returned)${strategy === "aggregate" && aggExpr ? " — superseded by aggregate expression" : ""}`}>
                <input
                  list={lookupTableColumns ? "tx-lookup-columns" : undefined}
                  value={(params.value_column as string) ?? ""}
                  onChange={(e) => updateParams({ value_column: e.target.value })}
                  style={inputStyle}
                  disabled={strategy === "aggregate" && !!aggExpr}
                  placeholder={lookupTableColumns ? "pick from the chosen table's columns" : "name"}
                />
              </Field>
            )}
            {strategy === "latest" && (
              <>
                <Field label="Order-by column (recency)">
                  <input
                    list={lookupTableColumns ? "tx-lookup-columns" : undefined}
                    value={(params.order_by_column as string) ?? ""}
                    onChange={(e) => updateParams({ order_by_column: e.target.value })}
                    style={inputStyle}
                    placeholder={lookupTableColumns ? "pick from the chosen table's columns" : "effective_date"}
                  />
                </Field>
                <Field label="Order direction">
                  <select
                    value={((params.order_by_direction as string) ?? "DESC").toUpperCase()}
                    onChange={(e) => updateParams({ order_by_direction: e.target.value })}
                    style={inputStyle}
                  >
                    <option value="DESC">DESC (newest first)</option>
                    <option value="ASC">ASC (oldest first)</option>
                  </select>
                </Field>
              </>
            )}
            {strategy === "aggregate" && (
              <>
                <Field label={`Aggregate function${aggExpr ? " — superseded by aggregate expression" : ""}`}>
                  <select
                    value={((params.aggregate_function as string) ?? _AGG_FN_DEFAULT).toUpperCase()}
                    onChange={(e) => updateParams({ aggregate_function: e.target.value })}
                    style={inputStyle}
                    disabled={!!aggExpr}
                  >
                    {LOOKUP_AGGREGATE_FUNCTIONS.map((fn) => (
                      <option key={fn} value={fn}>{fn}</option>
                    ))}
                  </select>
                </Field>
                <Field label="Aggregate expression (advanced — composite aggregates)">
                  <textarea
                    value={(params.aggregate_expression as string) ?? ""}
                    onChange={(e) => updateParams({ aggregate_expression: e.target.value })}
                    rows={3}
                    style={{
                      ...inputStyle, fontFamily: "ui-monospace, monospace",
                      fontSize: 12, width: "100%", boxSizing: "border-box",
                      resize: "vertical",
                    }}
                    placeholder="GREATEST(0, LEAST(100, (CURRENT_DATE - MAX(txn_timestamp)::date) * 2 - COUNT(txn_id) FILTER (WHERE txn_timestamp >= CURRENT_DATE - 90) * 5))"
                  />
                  <div style={{ fontSize: 11, color: "#94a3b8", marginTop: 2 }}>
                    Raw SQL over the lookup table's columns — may combine multiple
                    aggregates, each with its own <code>FILTER (WHERE …)</code>.
                    Emitted as <code>SELECT &lt;key&gt;, &lt;expression&gt; AS agg_value … GROUP BY &lt;key&gt;</code>;
                    supersedes the function/value-column pair when filled.
                  </div>
                </Field>
              </>
            )}
            <Field label="Filter clause (optional, advanced)">
              <input
                value={(params.filter_clause as string) ?? ""}
                onChange={(e) => updateParams({ filter_clause: e.target.value })}
                style={inputStyle}
                placeholder="is_active = true"
              />
            </Field>
          </>
        );
      }
      case "bucket":
        return (
          <>
            <Field label="Boundaries (comma-separated numeric thresholds)">
              <input
                value={Array.isArray(params.boundaries) ? (params.boundaries as number[]).join(", ") : ""}
                onChange={(e) => {
                  const parts = e.target.value.split(",").map((s) => s.trim()).filter(Boolean);
                  const nums = parts.map((s) => Number(s)).filter((n) => !Number.isNaN(n));
                  updateParams({ boundaries: nums });
                }}
                style={inputStyle}
                placeholder="25, 50, 100"
              />
            </Field>
            <Field label="Labels (comma-separated, N+1 for N boundaries)">
              <input
                value={Array.isArray(params.labels) ? (params.labels as string[]).join(", ") : ""}
                onChange={(e) => {
                  const parts = e.target.value.split(",").map((s) => s.trim()).filter(Boolean);
                  updateParams({ labels: parts });
                }}
                style={inputStyle}
                placeholder="low, medium, high, very_high"
              />
            </Field>
          </>
        );
      case "mask":
        return (
          <>
            <Field label="Algorithm">
              <select
                value={((params.algorithm as string) ?? "keep_last")}
                onChange={(e) => updateParams({ algorithm: e.target.value })}
                style={inputStyle}
              >
                <option value="keep_last">Keep last N (mask prefix)</option>
                <option value="keep_first">Keep first N (mask suffix)</option>
                <option value="middle">Keep first and last N (mask middle)</option>
              </select>
            </Field>
            <Field label="Keep N characters">
              <input
                type="number" min={1}
                value={(params.keep_n as number) ?? 4}
                onChange={(e) => updateParams({ keep_n: Number(e.target.value) })}
                style={inputStyle}
              />
            </Field>
            <Field label="Mask character">
              <input
                value={((params.mask_char as string) ?? "X").slice(0, 1)}
                maxLength={1}
                onChange={(e) => updateParams({ mask_char: e.target.value.slice(0, 1) || "X" })}
                style={{ ...inputStyle, width: 60 }}
              />
            </Field>
            <Field label="Keep non-alphanumeric chars (dashes, dots, spaces)">
              <input
                type="checkbox"
                checked={Boolean(params.keep_format)}
                onChange={(e) => updateParams({ keep_format: e.target.checked })}
              />
            </Field>
          </>
        );
      case "hash":
        return (
          <>
            <Field label="Algorithm">
              <select
                value={((params.algorithm as string) ?? "md5")}
                onChange={(e) => updateParams({ algorithm: e.target.value })}
                style={inputStyle}
              >
                <option value="md5">md5 (built-in)</option>
                <option value="sha1">sha1 (requires pgcrypto)</option>
                <option value="sha256">sha256 (requires pgcrypto)</option>
              </select>
            </Field>
            <Field label="Salt (optional namespace prefix)">
              <input
                value={(params.salt as string) ?? ""}
                onChange={(e) => updateParams({ salt: e.target.value })}
                style={inputStyle}
                placeholder="my-namespace"
              />
            </Field>
            <div style={{ fontSize: 11, color: "#b45309", backgroundColor: "#fef3c7", padding: "6px 8px", borderRadius: 4, border: "1px solid #fcd34d" }}>
              Not a cryptographic primitive. Salt here is a determinism / namespace tool, not a defense against rainbow-table attacks.
            </div>
          </>
        );
      case "literal":
        return (
          <>
            <Field label="Literal value (SQL literal — quote strings yourself)">
              <input
                value={(params.literal_value as string) ?? ""}
                onChange={(e) => updateParams({ literal_value: e.target.value })}
                style={inputStyle}
                placeholder="'USD'  or  42  or  true  or  NULL"
              />
            </Field>
            <Field label="Target SQL type (optional cast)">
              <input
                value={(params.target_type as string) ?? ""}
                onChange={(e) => updateParams({ target_type: e.target.value })}
                style={inputStyle}
                placeholder="char(3), numeric(10,2), ..."
              />
            </Field>
          </>
        );
      case "window": {
        const fn = ((params.function as string) || WINDOW_FUNCTION_DEFAULT).toUpperCase();
        const isLagLead = fn === "LAG" || fn === "LEAD";
        const isNtile = fn === "NTILE";
        return (
          <>
            <Field label="Function">
              <select
                value={fn}
                onChange={(e) => updateParams({ function: e.target.value })}
                style={inputStyle}
              >
                {WINDOW_FUNCTIONS.map((f) => (
                  <option key={f.value} value={f.value}>{f.label}</option>
                ))}
              </select>
            </Field>
            <Field label="Window name (declared on :DatasetTransform.window_specs)">
              <input
                value={(params.window as string) ?? ""}
                onChange={(e) => updateParams({ window: e.target.value })}
                style={inputStyle}
                placeholder="by_dept_hire_desc"
              />
            </Field>
            {isLagLead && (
              <>
                <Field label="Offset (LAG/LEAD — how many rows back/forward)">
                  <input
                    type="number" min={1}
                    value={(params.offset as number) ?? 1}
                    onChange={(e) => updateParams({ offset: Number(e.target.value) || 1 })}
                    style={inputStyle}
                  />
                </Field>
                <Field label="Default value (LAG/LEAD — used when no row exists at offset)">
                  <input
                    value={(params.default as string | number | boolean | null | undefined) == null
                      ? ""
                      : String(params.default)}
                    onChange={(e) => {
                      const v = e.target.value;
                      // Empty input clears the default; otherwise persist as-is
                      // (engineer's responsibility to quote strings if needed —
                      // view-DDL applies a string-quote heuristic).
                      if (v === "") {
                        const next = { ...params };
                        delete (next as Record<string, unknown>).default;
                        update({ transform_params: next });
                      } else {
                        updateParams({ default: v });
                      }
                    }}
                    style={inputStyle}
                    placeholder="0  or  'unknown'  or  leave empty for NULL"
                  />
                </Field>
              </>
            )}
            {isNtile && (
              <Field label="Buckets (NTILE — number of equal-size groups)">
                <input
                  type="number" min={2}
                  value={(params.ntile as number) ?? 4}
                  onChange={(e) => updateParams({ ntile: Number(e.target.value) || 4 })}
                  style={inputStyle}
                />
              </Field>
            )}
            <div style={{ fontSize: 11, color: "#475569", backgroundColor: "#f1f5f9", padding: "6px 8px", borderRadius: 4, border: "1px solid #cbd5e1" }}>
              The window name must match a declaration on this product's <code>:DatasetTransform.window_specs</code>
              (authored by the PO in the wizard's Shape the Schema → Window specs panel).
              View-DDL falls back to degenerate <code>OVER ()</code> if the name is unknown, and surfaces it as a warning on the serving stage callout.
            </div>
          </>
        );
      }
      case "date_difference": {
        const semantics = (params.semantics as string) || DATE_DIFF_SEMANTICS_DEFAULT;
        return (
          <>
            <Field label="Semantics (how the difference is counted — load-bearing)">
              <select
                value={semantics}
                onChange={(e) => updateParams({ unit: "year", semantics: e.target.value })}
                style={inputStyle}
              >
                {DATE_DIFF_SEMANTICS.map((s) => (
                  <option key={s.value} value={s.value}>{s.label}</option>
                ))}
              </select>
            </Field>
            <div style={{ fontSize: 11, color: "#475569", backgroundColor: "#f1f5f9", padding: "6px 8px", borderRadius: 4, border: "1px solid #cbd5e1" }}>
              Wire the <strong>start</strong> then <strong>end</strong> column via <em>Inputs</em> (order matters).
              A single input pairs with <code>CURRENT_DATE</code> — the age-from-date-of-birth case.
              The concrete SQL is resolved per served platform at build time (e.g. <code>AGE()</code> on
              Postgres, <code>TIMESTAMPDIFF()</code> on MySQL), so the mapping stays portable.
              {" "}<strong>Symbolic interval</strong> is Postgres-only — the preflight flags it on other engines.
              (v1 supports <code>unit=year</code>.)
            </div>
          </>
        );
      }
      default:
        return null;
    }
  };

  const isLiteral = value.transform_kind === "literal";

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10, padding: 12, backgroundColor: "#f8fafc", borderRadius: 6, border: "1px solid #e2e8f0" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
        <label style={labelStyle}>Kind</label>
        <select
          value={value.transform_kind}
          onChange={(e) => {
            const k = e.target.value as TransformKind;
            update({ transform_kind: k });
            if (k === "expression") setRawMode(true);
          }}
          style={{ ...inputStyle, width: 220 }}
          disabled={saving}
        >
          {KIND_OPTIONS.map((k) => (
            <option key={k} value={k}>{TRANSFORM_KIND_LABELS[k]}</option>
          ))}
        </select>
        {author && (
          <span style={{ fontSize: 11, color: "#64748b" }}>
            Author: <strong style={{ color: "#334155" }}>{TRANSFORM_AUTHOR_LABELS[author] ?? author}</strong>
            {confidence != null && ` (${confidence.toFixed(2)})`}
          </span>
        )}
      </div>

      {!isLiteral && (
      <div>
        <label style={labelStyle}>Inputs</label>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 4 }}>
          {sources.map((s, idx) => {
            const checked = s.uri ? value.transform_inputs.includes(s.uri) : false;
            return (
              <label
                key={s.uri ?? `${s.name ?? "src"}-${idx}`}
                style={{
                  display: "inline-flex", alignItems: "center", gap: 6,
                  padding: "4px 8px", borderRadius: 4,
                  border: `1px solid ${checked ? "#3b82f6" : "#cbd5e1"}`,
                  backgroundColor: checked ? "#eff6ff" : "#fff",
                  fontSize: 12, cursor: s.uri ? "pointer" : "default",
                }}
              >
                <input
                  type="checkbox"
                  checked={checked}
                  disabled={!s.uri || saving}
                  onChange={() => s.uri && toggleInput(s.uri)}
                  style={{ margin: 0 }}
                />
                <code style={{ fontSize: 11 }}>{s.name ?? "(unnamed)"}</code>
                {s.dataType && <span style={{ color: "#94a3b8", fontSize: 10 }}>{s.dataType}</span>}
              </label>
            );
          })}
        </div>
      </div>
      )}

      {renderParamFields()}

      {!isLiteral && (
      <div>
        <label style={labelStyle}>Decorators</label>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 10, marginTop: 4 }}>
          {STANDARDIZATION_OPTIONS.map((o) => (
            <label key={o.value} style={{ display: "inline-flex", alignItems: "center", gap: 4, fontSize: 12, color: "#334155" }}>
              <input
                type="checkbox"
                checked={standardization.includes(o.value)}
                onChange={(e) => setStandardization(o.value, e.target.checked)}
                disabled={saving}
              />
              {o.label}
            </label>
          ))}
          <Field label="Default if null">
            <input
              value={(decorators.default_if_null as string) ?? ""}
              onChange={(e) => setDefaultIfNull(e.target.value)}
              style={{ ...inputStyle, width: 140 }}
              placeholder="UNKNOWN"
              disabled={saving}
            />
          </Field>
        </div>
      </div>
      )}

      {!isLiteral && (
      <div>
        <label style={labelStyle}>Aggregation (when dataset has grouping_keys)</label>
        <div style={{ display: "flex", alignItems: "center", gap: 12, marginTop: 4, flexWrap: "wrap" }}>
          <label style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12, color: "#334155" }}>
            <input
              type="checkbox"
              checked={Boolean(value.grouping_key)}
              onChange={(e) => update({ grouping_key: e.target.checked, aggregate_function: e.target.checked ? "" : value.aggregate_function })}
              disabled={saving}
            />
            This column is a grouping key
          </label>
          {!value.grouping_key && (
            <Field label="Aggregate function">
              <select
                value={value.aggregate_function ?? ""}
                onChange={(e) => update({ aggregate_function: e.target.value })}
                style={{ ...inputStyle, width: 160 }}
                disabled={saving}
              >
                <option value="">(none — passthrough)</option>
                {COLUMN_AGGREGATE_FUNCTIONS.map((fn) => (
                  <option key={fn} value={fn}>{fn}</option>
                ))}
              </select>
            </Field>
          )}
          <span style={{ fontSize: 11, color: "#94a3b8", maxWidth: 360 }}>
            Only takes effect when the product dataset declares grouping_keys.
            For a grouped dataset, non-key columns must carry an aggregate function.
          </span>
        </div>
      </div>
      )}

      {mode === "expression" && (
        <>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <label style={{ ...labelStyle, marginBottom: 0 }}>SQL preview</label>
            {value.transform_kind !== "expression" && !isLiteral && (
              <button
                onClick={() => setRawMode((v) => !v)}
                style={{
                  fontSize: 11, padding: "2px 8px", borderRadius: 4,
                  border: "1px solid #cbd5e1", backgroundColor: "#fff",
                  color: "#475569", cursor: "pointer",
                }}
              >
                {rawMode ? "Hide raw SQL" : "Edit raw SQL"}
              </button>
            )}
          </div>
          <pre style={{
            margin: 0, padding: 8, backgroundColor: "#fff",
            border: "1px solid #e2e8f0", borderRadius: 4,
            fontSize: 12, fontFamily: "ui-monospace, monospace",
            color: "#334155", whiteSpace: "pre-wrap", wordBreak: "break-word",
          }}>{preview}</pre>

          {(rawMode || value.transform_kind === "expression") && !isLiteral && (
            <div>
              <label style={labelStyle}>Raw transform_expression (engineer override)</label>
              <textarea
                value={draftExpression ?? ""}
                onChange={(e) => setDraftExpression(e.target.value)}
                onBlur={() => update({ transform_expression: draftExpression ?? "" })}
                placeholder="first_name || ' ' || last_name"
                rows={2}
                style={{
                  ...inputStyle, fontFamily: "ui-monospace, monospace",
                  fontSize: 12, width: "100%", boxSizing: "border-box",
                  resize: "vertical",
                }}
                disabled={saving}
              />
              <div style={{ fontSize: 11, color: "#94a3b8", marginTop: 2 }}>
                Reference inputs by their bare column name. The view DDL generator
                substitutes them with aliased references via word-boundary matching.
              </div>
              <div style={{ marginTop: 8 }}>
                <label style={labelStyle}>Depends on product columns (optional)</label>
                <input
                  value={Array.isArray(value.transform_params?.depends_on_product_columns)
                    ? (value.transform_params.depends_on_product_columns as string[]).join(", ")
                    : ""}
                  onChange={(e) => {
                    const cols = e.target.value.split(",").map((s) => s.trim()).filter(Boolean);
                    updateParams({ depends_on_product_columns: cols.length ? cols : undefined });
                  }}
                  placeholder="last_txn_date, txn_count_90d"
                  style={{ ...inputStyle, width: "100%", boxSizing: "border-box" }}
                  disabled={saving}
                />
                <div style={{ fontSize: 11, color: "#94a3b8", marginTop: 2 }}>
                  Comma-separated names of OTHER product columns of this dataset that
                  the expression references. The view DDL computes this column in an
                  outer "enriched" CTE layer where those columns exist — use for
                  derived-on-derived calculations (e.g. a score combining two
                  aggregate columns).
                </div>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}

const labelStyle: React.CSSProperties = {
  display: "block", fontSize: 11, fontWeight: 600, color: "#475569",
  textTransform: "uppercase", letterSpacing: 0.4, marginBottom: 2,
};
const inputStyle: React.CSSProperties = {
  padding: "4px 8px", borderRadius: 4, border: "1px solid #cbd5e1",
  fontSize: 12, fontFamily: "inherit",
};

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <label style={labelStyle}>{label}</label>
      {children}
    </div>
  );
}
