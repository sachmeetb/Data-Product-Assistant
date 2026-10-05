import { useEffect, useState } from "react";
import api from "../api/client";

/** Per-consumer breakage row returned by /edit-impact. */
interface ConsumerImpact {
  consumer_uri: string;
  consumer_name: string;
  owner_email: string;
  consumer_state: string;
  affected_mappings: number;
  affected_columns: number;
  severity: "breaking" | "schema";
}

interface SchemaDiff {
  added: { schema: string; name: string }[];
  removed: { schema: string; name: string }[];
  type_changed: { schema: string; name: string; from: string; to: string }[];
  description_changed: { schema: string; name: string; from: string; to: string; from_empty: boolean }[];
  pii_added: { schema: string; name: string }[];
  pii_removed: { schema: string; name: string }[];
}

interface MetadataDiff {
  changed_fields: { field: string; key: string; from: string; to: string; from_empty: boolean }[];
}

interface ImpactResponse {
  has_impact: boolean;
  reason?: string;
  change_kind?: "cosmetic" | "schema" | "breaking";
  classifier_reasons?: string[];
  deployed_version?: number;
  current_version?: number;
  consumers?: ConsumerImpact[];
  schema_diff?: SchemaDiff;
  metadata_diff?: MetadataDiff;
}

interface Props {
  projectId: number;
  /** Initial change_kind hint from the system. The PO can override via the toggle. */
  resolvedKind: "cosmetic" | "schema" | "breaking" | null;
  /** Controlled revision-notes value bubbled to the parent so it can pass
   *  it to the save endpoint along with the override change_kind. */
  revisionNotes: string;
  onRevisionNotesChange: (v: string) => void;
  /** PO's override choice. ``null`` = follow the system recommendation. */
  changeKindOverride: "cosmetic" | "schema" | "breaking" | null;
  onChangeKindOverrideChange: (v: "cosmetic" | "schema" | "breaking" | null) => void;
}

const KIND_LABELS: Record<string, { label: string; subtext: string; color: string }> = {
  cosmetic: {
    label: "Cosmetic",
    subtext: "Patch in place — no version bump, consumers unaffected.",
    color: "#0a7d31",
  },
  schema: {
    label: "Schema change",
    subtext: "New version — non-breaking, consumers see a drift signal.",
    color: "#d97706",
  },
  breaking: {
    label: "Breaking change",
    subtext: "New version — consumers must rebind affected mappings.",
    color: "#b91c1c",
  },
};

export default function ImpactPreviewPanel({
  projectId,
  resolvedKind,
  revisionNotes,
  onRevisionNotesChange,
  changeKindOverride,
  onChangeKindOverrideChange,
}: Props) {
  const [impact, setImpact] = useState<ImpactResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    api
      .get<ImpactResponse>(`/api/projects/${projectId}/edit-impact`)
      .then((res) => {
        if (cancelled) return;
        setImpact(res.data);
      })
      .catch((e) => {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [projectId]);

  if (loading) {
    return (
      <div style={{ padding: 16, background: "#f8fafc", borderRadius: 8, marginBottom: 16 }}>
        Computing impact…
      </div>
    );
  }
  if (error) {
    return (
      <div style={{ padding: 16, background: "#fff5f5", border: "1px solid #fee2e2", borderRadius: 8, marginBottom: 16 }}>
        Impact preview failed: {error}
      </div>
    );
  }
  if (!impact) return null;

  // No-impact paths (never deployed yet, or no in-flight edit). Don't
  // render the panel — there's nothing useful to show.
  if (!impact.has_impact && impact.reason === "no_contract") return null;
  if (!impact.has_impact && impact.reason === "never_deployed") {
    return (
      <div style={{ padding: 12, background: "#eff6ff", border: "1px solid #bfdbfe", borderRadius: 8, marginBottom: 16, fontSize: 14 }}>
        This product hasn't been deployed yet, so the first save will just create v1.
      </div>
    );
  }
  if (!impact.has_impact) return null;

  const systemKind = impact.change_kind ?? resolvedKind ?? "schema";
  const effectiveKind = changeKindOverride ?? systemKind;
  const sys = KIND_LABELS[systemKind];
  const consumers = impact.consumers ?? [];
  const breaking_consumers = consumers.filter((c) => c.severity === "breaking");
  const schema_consumers = consumers.filter((c) => c.severity === "schema");

  return (
    <div
      style={{
        marginBottom: 16,
        padding: 16,
        background: "#ffffff",
        border: "1px solid #e5e7eb",
        borderRadius: 8,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 8 }}>
        <span
          style={{
            padding: "4px 10px",
            background: sys.color,
            color: "white",
            borderRadius: 999,
            fontWeight: 600,
            fontSize: 12,
            textTransform: "uppercase",
            letterSpacing: 0.4,
          }}
        >
          {sys.label}
        </span>
        <span style={{ fontSize: 14, color: "#374151" }}>{sys.subtext}</span>
      </div>

      {/* Classifier reasons */}
      {(impact.classifier_reasons?.length ?? 0) > 0 && (
        <ul style={{ margin: "8px 0", paddingLeft: 20, fontSize: 13, color: "#4b5563" }}>
          {impact.classifier_reasons!.map((r, i) => (
            <li key={i}>{r}</li>
          ))}
        </ul>
      )}

      {/* Per-consumer impact */}
      {consumers.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 6 }}>
            Downstream consumers affected:
          </div>
          <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
            <thead>
              <tr style={{ textAlign: "left", borderBottom: "1px solid #e5e7eb" }}>
                <th style={{ padding: "6px 4px" }}>Consumer</th>
                <th style={{ padding: "6px 4px" }}>Owner</th>
                <th style={{ padding: "6px 4px" }}>Affected mappings</th>
                <th style={{ padding: "6px 4px" }}>Severity</th>
              </tr>
            </thead>
            <tbody>
              {[...breaking_consumers, ...schema_consumers].map((c) => (
                <tr key={c.consumer_uri} style={{ borderBottom: "1px solid #f3f4f6" }}>
                  <td style={{ padding: "6px 4px" }}>{c.consumer_name}</td>
                  <td style={{ padding: "6px 4px", color: "#6b7280" }}>{c.owner_email || "—"}</td>
                  <td style={{ padding: "6px 4px" }}>{c.affected_mappings}</td>
                  <td style={{ padding: "6px 4px" }}>
                    <span
                      style={{
                        padding: "2px 8px",
                        background: c.severity === "breaking" ? "#fee2e2" : "#fef3c7",
                        color: c.severity === "breaking" ? "#991b1b" : "#92400e",
                        borderRadius: 12,
                        fontSize: 11,
                        fontWeight: 600,
                      }}
                    >
                      {c.severity}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* PO override toggle */}
      <div style={{ marginTop: 16, paddingTop: 12, borderTop: "1px solid #e5e7eb" }}>
        <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 6 }}>
          How should this save be handled?
        </div>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <button
            onClick={() => onChangeKindOverrideChange(null)}
            style={{
              padding: "6px 10px",
              fontSize: 12,
              background: changeKindOverride === null ? "#1f2937" : "#f3f4f6",
              color: changeKindOverride === null ? "white" : "#1f2937",
              border: "1px solid #d1d5db",
              borderRadius: 6,
              cursor: "pointer",
            }}
          >
            System recommends ({systemKind})
          </button>
          {(["cosmetic", "schema", "breaking"] as const).map((k) => (
            <button
              key={k}
              onClick={() => onChangeKindOverrideChange(k)}
              style={{
                padding: "6px 10px",
                fontSize: 12,
                background: changeKindOverride === k ? KIND_LABELS[k].color : "#f3f4f6",
                color: changeKindOverride === k ? "white" : "#1f2937",
                border: "1px solid #d1d5db",
                borderRadius: 6,
                cursor: "pointer",
              }}
            >
              Force {KIND_LABELS[k].label.toLowerCase()}
            </button>
          ))}
        </div>
        <div style={{ marginTop: 6, fontSize: 12, color: "#6b7280" }}>
          Effective: <strong>{effectiveKind}</strong> →{" "}
          {effectiveKind === "cosmetic"
            ? "patch the current version (no version bump)"
            : `branch v${(impact.current_version ?? 1) + 1}`}
        </div>
      </div>

      {/* Revision notes editor */}
      <div style={{ marginTop: 16 }}>
        <label
          htmlFor="revision-notes"
          style={{ display: "block", fontSize: 13, fontWeight: 600, marginBottom: 6 }}
        >
          Revision notes (markdown, surfaced to consumers)
        </label>
        <textarea
          id="revision-notes"
          value={revisionNotes}
          onChange={(e) => onRevisionNotesChange(e.target.value)}
          placeholder={
            effectiveKind === "cosmetic"
              ? "e.g. Fixed typo in column description."
              : effectiveKind === "breaking"
              ? "e.g. Dropped legacy 'value' column — replaced by 'amount'. Consumers must rebind."
              : "e.g. Added 'created_at' timestamp for downstream change-data-capture."
          }
          rows={3}
          style={{
            width: "100%",
            padding: 8,
            fontSize: 13,
            fontFamily: "inherit",
            borderRadius: 6,
            border: "1px solid #d1d5db",
            resize: "vertical",
          }}
        />
      </div>
    </div>
  );
}
