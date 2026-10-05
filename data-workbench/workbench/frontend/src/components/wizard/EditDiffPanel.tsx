import { useMemo, useState } from "react";
import {
  computeEditDiff,
  isEmptyDiff,
  summarizeImpact,
  type SpecSnapshot,
} from "../../lib/editDiffImpact";

interface Props {
  /** Snapshot of the deployed contract captured at hydration time. */
  deployed: SpecSnapshot | null;
  /** Live wizard state, derived per-render. */
  current: SpecSnapshot;
  /** When `true` renders as a compact final-review panel (used on the
   *  submit step). When `false` renders as a collapsible step-side panel. */
  reviewMode?: boolean;
  /** Initial collapsed state for non-review mode. */
  defaultCollapsed?: boolean;
}

export default function EditDiffPanel({ deployed, current, reviewMode = false, defaultCollapsed = false }: Props) {
  const [collapsed, setCollapsed] = useState(defaultCollapsed);
  const diff = useMemo(() => (deployed ? computeEditDiff(deployed, current) : null), [deployed, current]);

  if (!diff) return null;
  const empty = isEmptyDiff(diff);

  const containerStyle: React.CSSProperties = {
    margin: reviewMode ? "16px 0" : "12px 0",
    padding: reviewMode ? 16 : 12,
    borderRadius: 10,
    border: "1px solid #cbd5e1",
    backgroundColor: "#f8fafc",
  };

  if (reviewMode && empty) {
    return (
      <div style={containerStyle}>
        <div style={{ fontWeight: 700, fontSize: 14, color: "#475569" }}>
          No changes vs deployed version
        </div>
        <div style={{ fontSize: 13, color: "#64748b", marginTop: 6 }}>
          Submitting will be a no-op for engineering — consider canceling instead.
        </div>
      </div>
    );
  }

  const impact = summarizeImpact(diff);

  return (
    <div style={containerStyle}>
      <div
        onClick={() => !reviewMode && setCollapsed(!collapsed)}
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          cursor: reviewMode ? "default" : "pointer",
          marginBottom: collapsed ? 0 : 8,
        }}
      >
        <div style={{ fontWeight: 700, fontSize: 13, color: "#0f172a" }}>
          {reviewMode ? "Changes to submit" : "Changes vs deployed version"}
          {empty && !reviewMode && (
            <span style={{ marginLeft: 8, fontWeight: 500, color: "#94a3b8" }}>
              (no differences yet)
            </span>
          )}
        </div>
        {!reviewMode && (
          <span style={{ fontSize: 11, color: "#64748b" }}>{collapsed ? "Show" : "Hide"}</span>
        )}
      </div>

      {!collapsed && !empty && (
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {diff.columns.added.length > 0 && (
            <DiffSection
              label="Added columns"
              color="#16a34a"
              chips={diff.columns.added}
            />
          )}
          {diff.columns.removed.length > 0 && (
            <DiffSection
              label="Removed columns"
              color="#dc2626"
              chips={diff.columns.removed}
            />
          )}
          {diff.columns.typeChanged.length > 0 && (
            <DiffList
              label="Type changes"
              color="#d97706"
              items={diff.columns.typeChanged.map((c) => `${c.name}: ${c.from} → ${c.to}`)}
            />
          )}
          {diff.columns.descriptionChanged.length > 0 && (
            <DiffList
              label="Description changes"
              color="#0284c7"
              items={diff.columns.descriptionChanged.map((c) => `${c.name}`)}
            />
          )}
          {diff.metadata.changedFields.length > 0 && (
            <DiffList
              label="Metadata changes"
              color="#0284c7"
              items={diff.metadata.changedFields.map((m) => m.field)}
            />
          )}
          {/* Inputs section is consumer-aligned only — for other archetypes
              both arrays are empty and these blocks render nothing. Show
              source-product names rather than dprod URIs because the URI
              would dominate the chip width. */}
          {diff.inputs.added.length > 0 && (
            <DiffSection
              label="Added inputs"
              color="#16a34a"
              chips={diff.inputs.added.map((i) => i.name || i.dprod_uri)}
            />
          )}
          {diff.inputs.removed.length > 0 && (
            <DiffSection
              label="Removed inputs"
              color="#dc2626"
              chips={diff.inputs.removed.map((i) => i.name || i.dprod_uri)}
            />
          )}

          {impact.length > 0 && (
            <div
              style={{
                marginTop: 4,
                padding: 10,
                borderRadius: 8,
                backgroundColor: "#fff7ed",
                border: "1px solid #fed7aa",
              }}
            >
              <div style={{ fontWeight: 700, fontSize: 12, color: "#9a3412", marginBottom: 4 }}>
                Engineering impact
              </div>
              {impact.map((line, i) => (
                <div key={i} style={{ fontSize: 12, color: "#7c2d12", lineHeight: 1.5 }}>
                  • {line}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function DiffSection({ label, color, chips }: { label: string; color: string; chips: string[] }) {
  return (
    <div>
      <div style={{ fontSize: 12, fontWeight: 600, color, marginBottom: 4 }}>{label}</div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
        {chips.map((c) => (
          <span
            key={c}
            style={{
              fontSize: 11,
              padding: "2px 8px",
              borderRadius: 10,
              backgroundColor: "#fff",
              border: `1px solid ${color}`,
              color,
            }}
          >
            {c}
          </span>
        ))}
      </div>
    </div>
  );
}

function DiffList({ label, color, items }: { label: string; color: string; items: string[] }) {
  return (
    <div>
      <div style={{ fontSize: 12, fontWeight: 600, color, marginBottom: 4 }}>{label}</div>
      <ul style={{ margin: 0, paddingLeft: 18, color: "#334155", fontSize: 12, lineHeight: 1.6 }}>
        {items.map((it, i) => (
          <li key={i}>{it}</li>
        ))}
      </ul>
    </div>
  );
}
