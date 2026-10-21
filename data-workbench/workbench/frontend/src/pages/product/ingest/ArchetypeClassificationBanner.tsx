import { useState } from "react";
import type { IngestClassification } from "../../../types";

/**
 * Renders the data-product-archetype-classifier skill's recommendation
 * (kind + confidence + rationale + signals) with a toggle so the PO can
 * override the recommendation before continuing.
 *
 * The signals list is collapsed by default — it's the "show me why" detail
 * that the PO can expand if they want to vet the classifier's reasoning.
 */
export interface ArchetypeClassificationBannerProps {
  classification: IngestClassification | null;
  /** Loading flag — banner shows a skeleton instead of empty space. */
  loading: boolean;
  /**
   * The PO's chosen archetype. Equal to ``classification.kind`` initially,
   * but the PO can flip it via the override radio. The host page persists
   * this back to the draft as ``archetype_choice``.
   */
  archetypeChoice: "source" | "consumer";
  onArchetypeChange: (next: "source" | "consumer") => void;
}

export default function ArchetypeClassificationBanner({
  classification,
  loading,
  archetypeChoice,
  onArchetypeChange,
}: ArchetypeClassificationBannerProps) {
  const [showSignals, setShowSignals] = useState(false);

  if (loading) {
    return (
      <div style={{ ...container, borderColor: "#e2e8f0", backgroundColor: "#f8fafc" }}>
        <div style={{ fontSize: 13, color: "#64748b" }}>
          Analyzing the spec to determine if this is a source-aligned or
          consumer-aligned product…
        </div>
      </div>
    );
  }

  if (!classification) {
    return null;
  }

  const recommended = classification.kind;
  const isOverride = recommended !== archetypeChoice;
  const bgColor =
    recommended === "consumer" ? "#eef2ff" : "#fef3c7";
  const borderColor =
    recommended === "consumer" ? "#c7d2fe" : "#fde68a";
  const recommendedLabel =
    recommended === "consumer" ? "Consumer-aligned" : "Source-aligned";
  const recommendedColor =
    recommended === "consumer" ? "#4338ca" : "#854d0e";

  return (
    <div style={{ ...container, borderColor, backgroundColor: bgColor }}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 14, alignItems: "flex-start" }}>
        <div style={{ minWidth: 0 }}>
          <div style={{ fontSize: 11, fontWeight: 700, letterSpacing: 0.5, textTransform: "uppercase", color: "#475569" }}>
            Classifier recommendation
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 4 }}>
            <span style={{ fontSize: 16, fontWeight: 700, color: recommendedColor }}>
              {recommendedLabel}
            </span>
            <span
              style={{
                padding: "2px 8px",
                borderRadius: 999,
                backgroundColor: "#fff",
                border: `1px solid ${borderColor}`,
                fontSize: 11,
                fontWeight: 700,
                color: "#334155",
              }}
            >
              {classification.confidence}% confidence
            </span>
            {classification._fallback && (
              <span style={{ fontSize: 11, color: "#94a3b8", fontStyle: "italic" }}>
                heuristic fallback
              </span>
            )}
          </div>
          {classification.rationale && (
            <div style={{ fontSize: 12, color: "#334155", marginTop: 6, lineHeight: 1.5 }}>
              {classification.rationale}
            </div>
          )}
          {classification.signals && classification.signals.length > 0 && (
            <div style={{ marginTop: 6 }}>
              <button
                type="button"
                onClick={() => setShowSignals((v) => !v)}
                style={{
                  background: "transparent",
                  border: "none",
                  padding: 0,
                  color: "#475569",
                  fontSize: 11,
                  fontWeight: 600,
                  cursor: "pointer",
                  textDecoration: "underline",
                }}
              >
                {showSignals ? "Hide signals" : `Show ${classification.signals.length} signal${classification.signals.length === 1 ? "" : "s"}`}
              </button>
              {showSignals && (
                <ul style={{ margin: "6px 0 0 18px", padding: 0, fontSize: 11, color: "#475569" }}>
                  {classification.signals.map((s) => (
                    <li key={s}>{s}</li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 6, minWidth: 220 }}>
          <span style={{ fontSize: 11, fontWeight: 700, color: "#334155", letterSpacing: 0.4, textTransform: "uppercase" }}>
            I'm ingesting a…
          </span>
          <ArchetypeRadio
            label="Source-aligned product"
            description="Mirror of a system of record. No upstream dependencies."
            checked={archetypeChoice === "source"}
            onChange={() => onArchetypeChange("source")}
          />
          <ArchetypeRadio
            label="Consumer-aligned product"
            description="Composed from one or more source-aligned products."
            checked={archetypeChoice === "consumer"}
            onChange={() => onArchetypeChange("consumer")}
          />
          {isOverride && (
            <div style={{ fontSize: 11, color: "#92400e", marginTop: 4 }}>
              ⚠ You've overridden the classifier's recommendation.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function ArchetypeRadio({
  label,
  description,
  checked,
  onChange,
}: {
  label: string;
  description: string;
  checked: boolean;
  onChange: () => void;
}) {
  return (
    <label
      style={{
        display: "flex",
        gap: 8,
        cursor: "pointer",
        padding: 8,
        borderRadius: 6,
        backgroundColor: checked ? "#fff" : "transparent",
        border: `1px solid ${checked ? "#94a3b8" : "transparent"}`,
      }}
    >
      <input type="radio" checked={checked} onChange={onChange} style={{ marginTop: 2 }} />
      <span style={{ display: "flex", flexDirection: "column", gap: 2 }}>
        <span style={{ fontSize: 12, fontWeight: 700, color: "#0f172a" }}>{label}</span>
        <span style={{ fontSize: 11, color: "#64748b", lineHeight: 1.4 }}>{description}</span>
      </span>
    </label>
  );
}

const container: React.CSSProperties = {
  padding: 14,
  borderRadius: 10,
  border: "1px solid #c7d2fe",
  backgroundColor: "#eef2ff",
  marginBottom: 14,
};
