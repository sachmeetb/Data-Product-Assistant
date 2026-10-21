import { useEffect, useState } from "react";
import api from "../api/client";

interface SourceDrift {
  source_dprod_uri: string;
  source_name: string;
  source_product_kind: string;
  source_contract_id: string | null;
  consumed_version: number;
  source_current_version: number;
  source_current_state: string | null;
  drifted: boolean;
  classifier?: {
    kind: "cosmetic" | "schema" | "breaking";
    reasons: string[];
    columns_added: string[];
    columns_removed: string[];
    columns_type_changed: string[];
    columns_pii_added: string[];
  };
  revision_notes_since?: Array<{
    version: number;
    lifecycle_state: string | null;
    change_kind: string | null;
    revision_notes: string | null;
    published_at: string | null;
    actor: string | null;
    patches: Array<{
      occurred_at: string | null;
      change_kind: string | null;
      revision_notes: string | null;
      actor: string | null;
    }>;
  }>;
}

interface DriftResponse {
  project_id: number;
  drifted_count: number;
  sources: SourceDrift[];
}

interface Props {
  projectId: number;
}

const KIND_COLORS: Record<string, { bg: string; border: string; text: string }> = {
  cosmetic: { bg: "#f0fdf4", border: "#86efac", text: "#166534" },
  schema: { bg: "#fff7ed", border: "#fdba74", text: "#9a3412" },
  breaking: { bg: "#fef2f2", border: "#fca5a5", text: "#991b1b" },
};

export default function UpstreamDriftBanner({ projectId }: Props) {
  const [drift, setDrift] = useState<DriftResponse | null>(null);
  const [collapsed, setCollapsed] = useState(false);
  // Phase 5 pushback state: per-source toggle for the inline reason form,
  // per-source draft text, and a set of sources where pushback has been
  // submitted so the button flips to a "Submitted" chip without a refetch.
  const [pushbackOpenFor, setPushbackOpenFor] = useState<string | null>(null);
  const [pushbackDraft, setPushbackDraft] = useState<Record<string, string>>({});
  const [pushbackSubmitting, setPushbackSubmitting] = useState(false);
  const [pushbackSubmitted, setPushbackSubmitted] = useState<Set<string>>(new Set());
  const [pushbackError, setPushbackError] = useState<string | null>(null);

  const submitPushback = async (sourceContractId: string) => {
    const reason = (pushbackDraft[sourceContractId] || "").trim();
    if (!reason) {
      setPushbackError("Please describe why this change is a problem for your consumer.");
      return;
    }
    setPushbackSubmitting(true);
    setPushbackError(null);
    try {
      await api.post(`/api/projects/${projectId}/upstream-pushback`, {
        source_contract_id: sourceContractId,
        reason,
        severity: "schema",
      });
      setPushbackSubmitted((s) => new Set(s).add(sourceContractId));
      setPushbackOpenFor(null);
      setPushbackDraft((d) => ({ ...d, [sourceContractId]: "" }));
    } catch (e) {
      setPushbackError(e instanceof Error ? e.message : String(e));
    } finally {
      setPushbackSubmitting(false);
    }
  };

  useEffect(() => {
    let cancelled = false;
    api
      .get<DriftResponse>(`/api/projects/${projectId}/upstream-drift`)
      .then((res) => {
        if (cancelled) return;
        setDrift(res.data);
      })
      .catch(() => {
        if (cancelled) return;
        setDrift(null);
      });
    return () => {
      cancelled = true;
    };
  }, [projectId]);

  if (!drift || drift.drifted_count === 0) return null;

  const drifted = drift.sources.filter((s) => s.drifted);
  const worstKind =
    drifted.some((s) => s.classifier?.kind === "breaking")
      ? "breaking"
      : drifted.some((s) => s.classifier?.kind === "schema")
      ? "schema"
      : "cosmetic";
  const colors = KIND_COLORS[worstKind];

  return (
    <div
      style={{
        marginBottom: 16,
        padding: 12,
        background: colors.bg,
        border: `1px solid ${colors.border}`,
        borderRadius: 8,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span style={{ fontSize: 18 }}>⚠️</span>
          <strong style={{ color: colors.text, fontSize: 14 }}>
            {drifted.length} consumed source product
            {drifted.length !== 1 ? "s have" : " has"} a new version
          </strong>
        </div>
        <button
          onClick={() => setCollapsed((c) => !c)}
          style={{
            background: "transparent",
            border: "none",
            color: colors.text,
            cursor: "pointer",
            fontSize: 12,
          }}
        >
          {collapsed ? "Show details" : "Hide"}
        </button>
      </div>

      {!collapsed && (
        <div style={{ marginTop: 12, display: "flex", flexDirection: "column", gap: 12 }}>
          {drifted.map((s) => {
            const k = s.classifier?.kind;
            const kindColors = k ? KIND_COLORS[k] : null;
            return (
              <div
                key={s.source_dprod_uri}
                style={{
                  padding: 10,
                  background: "white",
                  borderRadius: 6,
                  borderLeft: `3px solid ${kindColors?.text ?? "#9ca3af"}`,
                }}
              >
                <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                  <strong style={{ fontSize: 14 }}>{s.source_name}</strong>
                  <span style={{ fontSize: 12, color: "#6b7280" }}>
                    you pinned v{s.consumed_version} → live v{s.source_current_version}
                  </span>
                  {k && (
                    <span
                      style={{
                        padding: "2px 8px",
                        background: kindColors!.text,
                        color: "white",
                        borderRadius: 12,
                        fontSize: 11,
                        fontWeight: 600,
                        textTransform: "uppercase",
                        letterSpacing: 0.3,
                      }}
                    >
                      {k}
                    </span>
                  )}
                </div>
                {s.classifier && (
                  <ul style={{ margin: "6px 0 0", paddingLeft: 18, fontSize: 12, color: "#374151" }}>
                    {s.classifier.reasons.map((r, i) => (
                      <li key={i}>{r}</li>
                    ))}
                    {s.classifier.columns_removed.length > 0 && (
                      <li>
                        Removed columns: <code>{s.classifier.columns_removed.join(", ")}</code>
                      </li>
                    )}
                    {s.classifier.columns_added.length > 0 && (
                      <li>
                        Added columns: <code>{s.classifier.columns_added.join(", ")}</code>
                      </li>
                    )}
                    {s.classifier.columns_type_changed.length > 0 && (
                      <li>
                        Type changes: <code>{s.classifier.columns_type_changed.join(", ")}</code>
                      </li>
                    )}
                    {s.classifier.columns_pii_added.length > 0 && (
                      <li>
                        Now flagged PII: <code>{s.classifier.columns_pii_added.join(", ")}</code>
                      </li>
                    )}
                  </ul>
                )}
                {(s.revision_notes_since?.length ?? 0) > 0 && (
                  <div style={{ marginTop: 8, fontSize: 12, color: "#4b5563" }}>
                    <strong>What the source PO said:</strong>
                    {s.revision_notes_since!.map((rn) => (
                      <div key={rn.version} style={{ marginTop: 4, paddingLeft: 8, borderLeft: "2px solid #e5e7eb" }}>
                        <span style={{ color: "#9ca3af" }}>v{rn.version}: </span>
                        {rn.revision_notes || <em style={{ color: "#9ca3af" }}>(no notes)</em>}
                        {rn.patches.length > 0 && (
                          <div style={{ marginLeft: 12, marginTop: 2, fontSize: 11, color: "#6b7280" }}>
                            + {rn.patches.length} cosmetic patch
                            {rn.patches.length !== 1 ? "es" : ""}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                )}
                <div style={{ marginTop: 8, fontSize: 12, display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
                  <a
                    href={`/engineer/projects/${projectId}#reviews`}
                    style={{ color: "#0369a1", textDecoration: "underline" }}
                  >
                    Review stale mappings →
                  </a>
                  {/* Phase 5 pushback affordance. When a consumer feels the
                      upstream change is breaking enough to warrant the source
                      PO revising it, they fire a :ProductRequest pushback. */}
                  {s.source_contract_id && (
                    pushbackSubmitted.has(s.source_contract_id) ? (
                      <span
                        style={{
                          padding: "2px 8px",
                          background: "#dcfce7",
                          color: "#166534",
                          borderRadius: 12,
                          fontSize: 11,
                          fontWeight: 600,
                        }}
                      >
                        Pushback submitted to source PO
                      </span>
                    ) : pushbackOpenFor === s.source_contract_id ? null : (
                      <button
                        onClick={() => setPushbackOpenFor(s.source_contract_id)}
                        style={{
                          padding: "4px 10px",
                          background: "white",
                          color: "#b91c1c",
                          border: "1px solid #fecaca",
                          borderRadius: 4,
                          fontSize: 12,
                          fontWeight: 600,
                          cursor: "pointer",
                        }}
                        title="Tell the source PO that this change breaks your use case"
                      >
                        Push back to source PO
                      </button>
                    )
                  )}
                </div>
                {pushbackOpenFor === s.source_contract_id && s.source_contract_id && (
                  <div style={{ marginTop: 8, padding: 10, background: "#fffbeb", border: "1px solid #fde68a", borderRadius: 6 }}>
                    <label style={{ display: "block", fontSize: 12, fontWeight: 600, marginBottom: 4 }}>
                      Why is this change a problem?
                    </label>
                    <textarea
                      value={pushbackDraft[s.source_contract_id] || ""}
                      onChange={(e) =>
                        setPushbackDraft({ ...pushbackDraft, [s.source_contract_id!]: e.target.value })
                      }
                      placeholder="e.g. Removing 'email' breaks our consumer's primary lookup path. We need 90 days notice or a transition column."
                      rows={3}
                      style={{
                        width: "100%",
                        padding: 8,
                        fontSize: 13,
                        fontFamily: "inherit",
                        border: "1px solid #d4d4d8",
                        borderRadius: 4,
                        resize: "vertical",
                        boxSizing: "border-box",
                      }}
                    />
                    <div style={{ marginTop: 6, display: "flex", gap: 8 }}>
                      <button
                        onClick={() => submitPushback(s.source_contract_id!)}
                        disabled={pushbackSubmitting}
                        style={{
                          padding: "4px 12px",
                          background: pushbackSubmitting ? "#9ca3af" : "#b91c1c",
                          color: "white",
                          border: "none",
                          borderRadius: 4,
                          fontSize: 12,
                          fontWeight: 600,
                          cursor: pushbackSubmitting ? "wait" : "pointer",
                        }}
                      >
                        {pushbackSubmitting ? "Submitting…" : "Send pushback"}
                      </button>
                      <button
                        onClick={() => setPushbackOpenFor(null)}
                        style={{
                          padding: "4px 12px",
                          background: "transparent",
                          color: "#374151",
                          border: "1px solid #d4d4d8",
                          borderRadius: 4,
                          fontSize: 12,
                          cursor: "pointer",
                        }}
                      >
                        Cancel
                      </button>
                    </div>
                    {pushbackError && (
                      <div style={{ marginTop: 6, fontSize: 12, color: "#b91c1c" }}>{pushbackError}</div>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
