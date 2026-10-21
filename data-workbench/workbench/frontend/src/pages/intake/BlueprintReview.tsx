/**
 * Controlled editor for an intake scaffold blueprint (migration or
 * modernization). High-confidence fields render accepted; low/missing ones are
 * highlighted for the reviewer to confirm/fill. Editing a value stamps it
 * confidence=high, review_state=edited (a human set it) AND auto-clears any gap
 * that pointed at that field — so a gap is resolved by *acting on its field*,
 * not by a disconnected button. "Mark resolved" remains as an explicit
 * acknowledgement for gaps whose inferred value is already fine as-is.
 * Product/dataset candidates can be excluded. All edits flow up via onChange —
 * the host page owns persistence.
 */
import { useState } from "react";
import ConfidenceChip from "../../components/ConfidenceChip";
import type {
  Blueprint,
  ConfidenceField,
  DatasetCandidate,
  Gap,
  MigrationBlueprint,
  ModernizationBlueprint,
  ProductCandidate,
} from "./types";

interface Props {
  blueprint: Blueprint;
  onChange: (next: Blueprint) => void;
  readOnly?: boolean;
  accent: string;
}

const clone = <T,>(v: T): T => JSON.parse(JSON.stringify(v));

const inputStyle: React.CSSProperties = {
  padding: "7px 9px",
  borderRadius: 6,
  border: "1px solid #cbd5e1",
  fontSize: 13,
  width: "100%",
  boxSizing: "border-box",
};
const cardStyle: React.CSSProperties = {
  border: "1px solid #e2e8f0",
  borderRadius: 10,
  padding: 14,
  background: "#fff",
};
const labelStyle: React.CSSProperties = {
  fontSize: 11,
  fontWeight: 700,
  color: "#64748b",
  textTransform: "uppercase",
  letterSpacing: 0.4,
};

// DOM id a gap uses to scroll to / flash the field it references.
const fieldDomId = (field: string) => `gap-field-${field}`;

function GradedField({
  label,
  fieldKey,
  field,
  onChange,
  readOnly,
  hasGap,
  flash,
}: {
  label: string;
  fieldKey: string;
  field?: ConfidenceField;
  onChange: (next: ConfidenceField) => void;
  readOnly?: boolean;
  hasGap?: boolean;
  flash?: boolean;
}) {
  const cf: ConfidenceField = field || { value: null, confidence: "missing" };
  const missing = (cf.confidence || "missing") === "missing" || !cf.value;
  const ring = flash ? "0 0 0 3px #fcd34d" : hasGap ? "0 0 0 2px #fde68a" : "none";
  return (
    <div id={fieldDomId(fieldKey)} style={{ display: "flex", flexDirection: "column", gap: 4, borderRadius: 8, boxShadow: ring, transition: "box-shadow 0.3s", padding: ring === "none" ? 0 : 4 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <span style={labelStyle}>{label}</span>
        <ConfidenceChip confidence={cf.confidence} compact />
        {hasGap ? <span style={{ fontSize: 10, color: "#b45309", fontWeight: 600 }}>needs review</span> : null}
      </div>
      <input
        style={{ ...inputStyle, borderColor: missing ? "#fca5a5" : "#cbd5e1", background: missing ? "#fff7f7" : "#fff" }}
        value={cf.value ?? ""}
        placeholder={missing ? "— missing; please fill —" : ""}
        disabled={readOnly}
        onChange={(e) =>
          onChange({ value: e.target.value || null, confidence: "high", why: "edited by reviewer", review_state: "edited" })
        }
      />
      {cf.why ? <div style={{ fontSize: 11, color: "#94a3b8" }}>{cf.why}</div> : null}
    </div>
  );
}

function GapList({
  gaps,
  onResolve,
  onFocus,
  readOnly,
}: {
  gaps?: Gap[];
  onResolve: (field: string) => void;
  onFocus: (field: string) => void;
  readOnly?: boolean;
}) {
  if (!gaps || gaps.length === 0) return null;
  return (
    <div style={{ ...cardStyle, background: "#fef2f2", borderColor: "#fecaca" }}>
      <div style={{ ...labelStyle, color: "#991b1b", marginBottom: 4 }}>
        Unresolved gaps ({gaps.length}) — resolve all before scaffolding
      </div>
      <div style={{ fontSize: 12, color: "#b91c1c", marginBottom: 8 }}>
        Each gap points at a field above. Click a gap to jump to it, then either <strong>edit the
        field</strong> (that clears the gap automatically) or, if the inferred value is already
        correct, click <strong>Accept as-is</strong>.
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        {gaps.map((g, i) => (
          <div key={i} style={{ display: "flex", alignItems: "center", gap: 10, fontSize: 13 }}>
            <button
              onClick={() => onFocus(g.field)}
              style={{ fontWeight: 600, color: "#7f1d1d", background: "none", border: "none", cursor: "pointer", padding: 0, textDecoration: "underline", fontFamily: "monospace", fontSize: 12 }}
            >
              {g.field}
            </button>
            <span style={{ color: "#b91c1c", flex: 1 }}>{g.why}</span>
            {!readOnly && (
              <button
                onClick={() => onResolve(g.field)}
                style={{ fontSize: 12, padding: "3px 8px", borderRadius: 6, border: "1px solid #fca5a5", background: "#fff", color: "#991b1b", cursor: "pointer", whiteSpace: "nowrap" }}
              >
                Accept as-is
              </button>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

function DatasetTable({
  datasets,
  editable,
  onCursorChange,
  gapFields,
  flashField,
}: {
  datasets?: DatasetCandidate[];
  editable?: boolean;
  onCursorChange?: (index: number, next: ConfidenceField) => void;
  gapFields?: Set<string>;
  flashField?: string | null;
}) {
  if (!datasets || datasets.length === 0) return null;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      {datasets.map((ds, i) => {
        const cursorKey = `datasets[${ds.candidate_id}].incremental_cursor`;
        const cursorGap = gapFields?.has(cursorKey);
        const cf = ds.incremental_cursor || { value: null, confidence: "missing" as const };
        return (
          <div key={ds.candidate_id || i} style={{ ...cardStyle, padding: 10 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
              <span style={{ fontWeight: 600, fontFamily: "monospace", fontSize: 13 }}>{ds.name?.value || "(unnamed)"}</span>
              <ConfidenceChip confidence={ds.name?.confidence} compact />
            </div>
            {ds.columns && ds.columns.length > 0 && (
              <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                {ds.columns.map((c, j) => (
                  <span key={c.candidate_id || j} style={{ fontSize: 11, background: "#f1f5f9", border: "1px solid #e2e8f0", borderRadius: 4, padding: "2px 6px", fontFamily: "monospace" }}>
                    {c.name?.value}
                    {c.data_type?.value ? <span style={{ color: "#94a3b8" }}> : {c.data_type.value}</span> : null}
                  </span>
                ))}
              </div>
            )}
            {editable && onCursorChange ? (
              <div
                id={fieldDomId(cursorKey)}
                style={{ marginTop: 8, display: "flex", alignItems: "center", gap: 8, borderRadius: 8, boxShadow: flashField === cursorKey ? "0 0 0 3px #fcd34d" : cursorGap ? "0 0 0 2px #fde68a" : "none", transition: "box-shadow 0.3s", padding: cursorGap || flashField === cursorKey ? 4 : 0 }}
              >
                <span style={{ ...labelStyle, whiteSpace: "nowrap" }}>Incremental cursor</span>
                <input
                  style={{ ...inputStyle, width: 200 }}
                  value={cf.value ?? ""}
                  placeholder="— none (one-time full load) —"
                  onChange={(e) =>
                    onCursorChange(i, { value: e.target.value || null, confidence: "high", why: "edited by reviewer", review_state: "edited" })
                  }
                />
                {cursorGap ? <span style={{ fontSize: 10, color: "#b45309", fontWeight: 600 }}>needs review</span> : null}
              </div>
            ) : ds.incremental_cursor?.value ? (
              <div style={{ fontSize: 11, color: "#64748b", marginTop: 6 }}>cursor: {ds.incremental_cursor.value}</div>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

function ProductCandidateCard({
  cand,
  onChange,
  readOnly,
  kindLabel,
  accent,
}: {
  cand: ProductCandidate;
  onChange: (next: ProductCandidate) => void;
  readOnly?: boolean;
  kindLabel: string;
  accent: string;
}) {
  const excluded = cand.review_state === "excluded";
  const set = (patch: Partial<ProductCandidate>) => onChange({ ...cand, ...patch });
  const setName = (v: string) =>
    set({ name: { value: v || null, confidence: "high", review_state: "edited", why: "edited by reviewer" } });
  return (
    <div style={{ ...cardStyle, opacity: excluded ? 0.5 : 1, borderColor: excluded ? "#e2e8f0" : "#c7d2fe" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
        <span style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: 0.4, color: accent }}>{kindLabel}</span>
        <ConfidenceChip confidence={cand.confidence || cand.name?.confidence} compact />
        <div style={{ flex: 1 }} />
        {!readOnly && (
          <button
            onClick={() => set({ review_state: excluded ? "suggested" : "excluded" })}
            style={{ fontSize: 12, padding: "3px 10px", borderRadius: 6, border: "1px solid #cbd5e1", background: excluded ? accent : "#fff", color: excluded ? "#fff" : "#334155", cursor: "pointer" }}
          >
            {excluded ? "Include" : "Exclude"}
          </button>
        )}
      </div>
      <input style={inputStyle} value={cand.name?.value ?? ""} disabled={readOnly || excluded} onChange={(e) => setName(e.target.value)} />
      <div style={{ display: "flex", gap: 12, marginTop: 6, fontSize: 12, color: "#64748b" }}>
        {cand.domain?.value ? <span>domain: {cand.domain.value}</span> : null}
        {cand.odcs ? <span style={{ color: "#4338ca" }}>has ODCS draft</span> : null}
      </div>
      {(cand.purpose || cand.product_idea || cand.description) && (
        <div style={{ fontSize: 12, color: "#475569", marginTop: 6 }}>{cand.purpose || cand.product_idea || cand.description}</div>
      )}
      <DatasetTable datasets={cand.datasets} />
    </div>
  );
}

export default function BlueprintReview({ blueprint, onChange, readOnly, accent }: Props) {
  const [flash, setFlash] = useState<string | null>(null);
  const emit = (next: Blueprint) => onChange(next);

  // Jump to and briefly highlight the field a gap references.
  const focusGap = (field: string) => {
    const el = document.getElementById(fieldDomId(field));
    if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
    setFlash(field);
    window.setTimeout(() => setFlash((f) => (f === field ? null : f)), 1400);
  };

  const gapFields = new Set((blueprint.gaps || []).map((g) => g.field));

  // Editable narrative summary — the blueprint `rationale`. Writes through the
  // same onChange path (persisted via PATCH /blueprint by the host page).
  const setRationale = (value: string) => {
    const c = clone(blueprint);
    (c as unknown as { rationale: string }).rationale = value;
    emit(c);
  };
  const RationaleEditor = (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      <div style={labelStyle}>Summary</div>
      {readOnly ? (
        <div style={{ fontSize: 13, color: "#475569", fontStyle: "italic" }}>
          {blueprint.rationale || "—"}
        </div>
      ) : (
        <textarea
          value={blueprint.rationale || ""}
          onChange={(e) => setRationale(e.target.value)}
          placeholder="Narrative summary of what the assessment recommended…"
          rows={3}
          style={{ ...inputStyle, resize: "vertical", fontStyle: "italic" }}
        />
      )}
    </div>
  );

  const Header = (
    <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 4 }}>
      <span style={{ fontSize: 12, fontWeight: 700, textTransform: "uppercase", letterSpacing: 0.5, color: accent }}>
        {blueprint.scenario}
      </span>
      <span style={{ fontSize: 12, color: "#64748b" }}>overall</span>
      <ConfidenceChip confidence={blueprint.overall_confidence} />
    </div>
  );

  if (blueprint.scenario === "migration") {
    const bp = blueprint as MigrationBlueprint;
    // Remove any gap that points at a given field key (a human just acted on it).
    const resolveGap = (c: MigrationBlueprint, key: string) => {
      if (c.gaps && c.gaps.length) c.gaps = c.gaps.filter((g) => g.field !== key);
    };
    const setCF = (key: keyof MigrationBlueprint) => (next: ConfidenceField) => {
      const c = clone(bp);
      (c as unknown as Record<string, ConfidenceField>)[key as string] = next;
      resolveGap(c, key as string);
      emit(c);
    };
    const setCursor = (index: number, next: ConfidenceField) => {
      const c = clone(bp);
      const ds = (c.datasets || [])[index];
      if (!ds) return;
      ds.incremental_cursor = next;
      resolveGap(c, `datasets[${ds.candidate_id}].incremental_cursor`);
      emit(c);
    };
    const gf = (key: string) => ({ hasGap: gapFields.has(key), flash: flash === key });
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        {Header}
        {RationaleEditor}
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
          <GradedField label="Project name" fieldKey="project_name" field={bp.project_name} onChange={setCF("project_name")} readOnly={readOnly} {...gf("project_name")} />
          <GradedField label="Domain" fieldKey="domain" field={bp.domain} onChange={setCF("domain")} readOnly={readOnly} {...gf("domain")} />
          <GradedField label="Source platform" fieldKey="source_platform" field={bp.source_platform} onChange={setCF("source_platform")} readOnly={readOnly} {...gf("source_platform")} />
          <GradedField label="Target platform" fieldKey="target_platform" field={bp.target_platform} onChange={setCF("target_platform")} readOnly={readOnly} {...gf("target_platform")} />
          <GradedField label="Write disposition" fieldKey="write_disposition" field={bp.write_disposition} onChange={setCF("write_disposition")} readOnly={readOnly} {...gf("write_disposition")} />
        </div>
        <div>
          <div style={{ ...labelStyle, marginBottom: 6 }}>Datasets ({(bp.datasets || []).length})</div>
          <DatasetTable datasets={bp.datasets} editable={!readOnly} onCursorChange={setCursor} gapFields={gapFields} flashField={flash} />
        </div>
        <GapList
          gaps={bp.gaps}
          readOnly={readOnly}
          onFocus={focusGap}
          onResolve={(field) => {
            const c = clone(bp);
            c.gaps = (c.gaps || []).filter((g) => g.field !== field);
            emit(c);
          }}
        />
      </div>
    );
  }

  const bp = blueprint as ModernizationBlueprint;
  // Resolve a candidate id to its display name (matches the editable name field) so the
  // Dependencies section reads "Customer Lending Exposure consumes → Cards" rather than raw ids.
  const nameById = new Map<string, string>();
  for (const c of [...(bp.source_aligned || []), ...(bp.consumer_aligned || [])]) {
    if (c.candidate_id) nameById.set(c.candidate_id, c.name?.value || c.candidate_id);
  }
  const depName = (id?: string | null) => (id && nameById.get(id)) || id || "?";
  const setCandidate = (arr: "source_aligned" | "consumer_aligned", idx: number) => (next: ProductCandidate) => {
    const c = clone(bp);
    const list = (c[arr] || []) as ProductCandidate[];
    list[idx] = next;
    c[arr] = list;
    emit(c);
  };
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      {Header}
      {RationaleEditor}
      <div>
        <div style={{ ...labelStyle, marginBottom: 6 }}>Source-aligned products ({(bp.source_aligned || []).length})</div>
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {(bp.source_aligned || []).map((cand, i) => (
            <ProductCandidateCard key={cand.candidate_id || i} cand={cand} onChange={setCandidate("source_aligned", i)} readOnly={readOnly} kindLabel="source-aligned" accent={accent} />
          ))}
        </div>
      </div>
      <div>
        <div style={{ ...labelStyle, marginBottom: 6 }}>Consumer-aligned products ({(bp.consumer_aligned || []).length})</div>
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {(bp.consumer_aligned || []).map((cand, i) => (
            <ProductCandidateCard key={cand.candidate_id || i} cand={cand} onChange={setCandidate("consumer_aligned", i)} readOnly={readOnly} kindLabel="consumer-aligned" accent={accent} />
          ))}
        </div>
      </div>
      {bp.dependencies && bp.dependencies.length > 0 && (() => {
        // Group dependencies by the consuming (from) product so it reads as a per-consumer list.
        const groups = new Map<string, { label: string; external: boolean }[]>();
        for (const d of bp.dependencies) {
          const arr = groups.get(d.from_candidate_id) || [];
          if (d.to_candidate_id) arr.push({ label: depName(d.to_candidate_id), external: false });
          else if (d.to_external_uri) arr.push({ label: d.to_external_uri, external: true });
          else arr.push({ label: "?", external: false });
          groups.set(d.from_candidate_id, arr);
        }
        return (
          <div>
            <div style={{ ...labelStyle, marginBottom: 6 }}>Dependencies ({bp.dependencies.length})</div>
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              {[...groups.entries()].map(([fromId, targets]) => (
                <div key={fromId} style={{ ...cardStyle, padding: 10 }}>
                  <div style={{ fontSize: 13, fontWeight: 600, color: "#4338ca", marginBottom: 4 }}>
                    {depName(fromId)}
                  </div>
                  <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                    {targets.map((t, i) => (
                      <span key={i} style={{ fontSize: 12, background: "#f5f3ff", border: "1px solid #ddd6fe", borderRadius: 6, padding: "3px 8px" }}>
                        consumes → {t.label}{t.external ? " (published)" : ""}
                      </span>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          </div>
        );
      })()}
      <GapList
        gaps={bp.gaps}
        readOnly={readOnly}
        onFocus={focusGap}
        onResolve={(field) => {
          const c = clone(bp);
          c.gaps = (c.gaps || []).filter((g) => g.field !== field);
          emit(c);
        }}
      />
    </div>
  );
}
