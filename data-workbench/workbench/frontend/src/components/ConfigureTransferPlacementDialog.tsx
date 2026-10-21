import { useEffect, useState } from "react";
import api from "../api/client";
import { useNotify } from "./dialogContext";
import type { StageInfo } from "../types";

interface OpAssignment {
  kind: string;
  side: "extract" | "target";
  reason: string;
  column?: string | null;
  dataset?: string | null;
  forced?: boolean;
}
interface Driver {
  code: string;
  severity: "required" | "consider" | "info";
  side?: "extract" | "target";
  label: string;
  detail: string;
}
interface PlacementOption {
  id: string;
  label: string;
  description: string;
}
interface PlacementAdvice {
  recommended_placement: string;
  chosen_placement: string;
  effective_placement: string;
  extract_ops: OpAssignment[];
  target_ops: OpAssignment[];
  forced_pre_boundary: OpAssignment[];
  per_op_assignments: OpAssignment[];
  warnings: string[];
  drivers: Driver[];
  placements: PlacementOption[];
  rationale?: string;
  considerations?: string[];
  source?: string;
  advisor_error?: string | null;
  source_platform?: string;
  target_platform?: string;
}

interface Props {
  open: boolean;
  projectId: number;
  stage: StageInfo | null;
  onClose: () => void;
  onCompleted: () => void;
}

const SIDE_STYLE: Record<string, { bg: string; fg: string; label: string }> = {
  extract: { bg: "#f5f3ff", fg: "#6d28d9", label: "Extract (source)" },
  target: { bg: "#f0fdf4", fg: "#15803d", label: "Target (after load)" },
};
const SEVERITY_STYLE: Record<string, { bg: string; fg: string }> = {
  required: { bg: "#fef2f2", fg: "#b91c1c" },
  consider: { bg: "#fffbeb", fg: "#b45309" },
  info: { bg: "#eff6ff", fg: "#1d4ed8" },
};

export default function ConfigureTransferPlacementDialog({
  open, projectId, stage, onClose, onCompleted,
}: Props) {
  const { showError } = useNotify();
  const [advice, setAdvice] = useState<PlacementAdvice | null>(null);
  const [chosen, setChosen] = useState<string>("");
  const [loading, setLoading] = useState(false);
  const [recomputing, setRecomputing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Initial load: full advice incl. the AI rationale narrative.
  useEffect(() => {
    if (!open || !stage) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    setAdvice(null);
    (async () => {
      try {
        const { data } = await api.post<PlacementAdvice>(
          `/api/projects/${projectId}/transform-placement/advise`, {},
        );
        if (cancelled) return;
        setAdvice(data);
        setChosen(data.chosen_placement || data.recommended_placement);
      } catch (e) {
        if (!cancelled) setError(`Failed to analyze placement: ${(e as Error).message || e}`);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [open, stage, projectId]);

  // Recompute the split when the engineer picks a different placement (fast; no skill).
  const pickPlacement = async (placement: string) => {
    setChosen(placement);
    setRecomputing(true);
    try {
      const { data } = await api.post<PlacementAdvice>(
        `/api/projects/${projectId}/transform-placement/advise`,
        { placement, skip_skill: true },
      );
      // Keep the richer initial rationale/considerations; refresh the split.
      setAdvice((prev) => prev ? {
        ...data,
        rationale: prev.rationale || data.rationale,
        considerations: prev.considerations?.length ? prev.considerations : data.considerations,
      } : data);
    } catch {
      /* leave the previous split in place on a transient error */
    } finally {
      setRecomputing(false);
    }
  };

  const handleConfirm = async () => {
    if (!stage || !chosen) return;
    setSaving(true);
    try {
      await api.put(`/api/projects/${projectId}/transform-placement/decision`, {
        placement: chosen,
      });
      if (stage.status === "pending" || stage.status === "running") {
        await api.post(
          `/api/projects/${projectId}/stages/${stage.stage_number}/complete`,
          null, { params: { workflow_id: stage.workflow_id } },
        );
      }
      onCompleted();
    } catch (e) {
      showError(e, { title: "Failed to save placement" });
    } finally {
      setSaving(false);
    }
  };

  if (!open || !stage) return null;

  const ops = advice ? [...advice.extract_ops, ...advice.target_ops] : [];
  const opLabel = (o: OpAssignment) =>
    o.column ? `${o.kind} · ${o.column}` : `${o.kind}${o.dataset ? ` · ${o.dataset}` : ""}`;

  return (
    <div
      onClick={onClose}
      style={{
        position: "fixed", inset: 0, backgroundColor: "rgba(15, 23, 42, 0.55)",
        display: "flex", alignItems: "center", justifyContent: "center", zIndex: 100,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: "#fff", borderRadius: 12, width: "min(720px, 94vw)",
          maxHeight: "90vh", overflowY: "auto", padding: 24,
          boxShadow: "0 20px 50px rgba(15,23,42,0.35)",
        }}
      >
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
          <h2 style={{ margin: 0, fontSize: 20, fontWeight: 700, color: "#0f172a" }}>
            Configure Transform Placement
          </h2>
          {advice && (
            <span style={{ fontSize: 13, color: "#64748b" }}>
              {advice.source_platform} → {advice.target_platform}
            </span>
          )}
        </div>
        <p style={{ marginTop: 6, marginBottom: 16, color: "#475569", fontSize: 13.5 }}>
          Choose where each transform runs when this product is moved across platforms —
          on the <b>source</b> before the move (ETL), on the <b>target</b> after load (ELT),
          or split (hybrid). The advisor recommends a placement per op; you decide.
        </p>

        {loading && <div style={{ padding: 24, color: "#64748b" }}>Analyzing the product's transforms…</div>}
        {error && <div style={{ padding: 16, color: "#b91c1c", background: "#fef2f2", borderRadius: 8 }}>{error}</div>}

        {advice && !loading && (
          <>
            {/* Placement options */}
            <div style={{ display: "flex", flexDirection: "column", gap: 8, marginBottom: 16 }}>
              {advice.placements.map((p) => {
                const active = chosen === p.id;
                const recommended = advice.recommended_placement === p.id;
                return (
                  <label key={p.id}
                    style={{
                      display: "flex", gap: 10, padding: "10px 12px", borderRadius: 8,
                      border: `1.5px solid ${active ? "#6366f1" : "#e2e8f0"}`,
                      background: active ? "#eef2ff" : "#fff", cursor: "pointer",
                    }}>
                    <input type="radio" name="placement" checked={active}
                      onChange={() => pickPlacement(p.id)} style={{ marginTop: 3 }} />
                    <div>
                      <div style={{ fontWeight: 600, color: "#0f172a" }}>
                        {p.label}
                        {recommended && (
                          <span style={{
                            marginLeft: 8, fontSize: 11, fontWeight: 700, color: "#4338ca",
                            background: "#e0e7ff", borderRadius: 999, padding: "1px 8px",
                          }}>Recommended</span>
                        )}
                      </div>
                      <div style={{ fontSize: 12.5, color: "#64748b" }}>{p.description}</div>
                    </div>
                  </label>
                );
              })}
            </div>

            {/* Per-op breakdown */}
            <div style={{ marginBottom: 14, opacity: recomputing ? 0.5 : 1 }}>
              <div style={{ fontWeight: 600, fontSize: 13, color: "#334155", marginBottom: 6 }}>
                Where each transform runs
              </div>
              {ops.length === 0 && (
                <div style={{ fontSize: 13, color: "#64748b" }}>
                  No authored transforms — the move is a straight copy.
                </div>
              )}
              <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                {ops.map((o, i) => {
                  const s = SIDE_STYLE[o.side];
                  return (
                    <div key={i} style={{
                      display: "flex", alignItems: "center", gap: 10, padding: "6px 10px",
                      background: "#f8fafc", borderRadius: 6, fontSize: 13,
                    }}>
                      <span style={{
                        background: s.bg, color: s.fg, fontWeight: 600, fontSize: 11.5,
                        borderRadius: 6, padding: "2px 8px", minWidth: 128, textAlign: "center",
                      }}>
                        {o.forced ? "🔒 " : ""}{s.label}
                      </span>
                      <span style={{ fontWeight: 600, color: "#0f172a" }}>{opLabel(o)}</span>
                      <span style={{ color: "#64748b", fontSize: 12 }}>{o.reason}</span>
                    </div>
                  );
                })}
              </div>
            </div>

            {/* Drivers */}
            {advice.drivers.length > 0 && (
              <div style={{ marginBottom: 14 }}>
                <div style={{ fontWeight: 600, fontSize: 13, color: "#334155", marginBottom: 6 }}>
                  Why
                </div>
                <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                  {advice.drivers.map((d) => {
                    const sv = SEVERITY_STYLE[d.severity] || SEVERITY_STYLE.info;
                    return (
                      <div key={d.code} style={{ fontSize: 12.5, color: "#475569" }}>
                        <span style={{
                          background: sv.bg, color: sv.fg, fontWeight: 700, fontSize: 10.5,
                          borderRadius: 999, padding: "1px 7px", marginRight: 6, textTransform: "uppercase",
                        }}>{d.severity}</span>
                        <b>{d.label}.</b> {d.detail}
                      </div>
                    );
                  })}
                </div>
              </div>
            )}

            {/* Rationale / considerations */}
            {advice.rationale && (
              <div style={{
                fontSize: 13, color: "#334155", background: "#f1f5f9",
                borderRadius: 8, padding: "10px 12px", marginBottom: 6,
              }}>
                {advice.rationale}
                {advice.considerations && advice.considerations.length > 0 && (
                  <ul style={{ margin: "8px 0 0", paddingLeft: 18 }}>
                    {advice.considerations.map((c, i) => <li key={i}>{c}</li>)}
                  </ul>
                )}
              </div>
            )}
            {advice.warnings?.length > 0 && (
              <div style={{ fontSize: 12, color: "#b45309" }}>
                {advice.warnings.map((w, i) => <div key={i}>⚠ {w}</div>)}
              </div>
            )}
          </>
        )}

        {/* Footer */}
        <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 20 }}>
          <button onClick={onClose} disabled={saving}
            style={{
              padding: "8px 16px", borderRadius: 8, border: "1px solid #cbd5e1",
              background: "#fff", color: "#334155", cursor: "pointer", fontWeight: 600,
            }}>
            Cancel
          </button>
          <button onClick={handleConfirm} disabled={saving || loading || !chosen}
            style={{
              padding: "8px 16px", borderRadius: 8, border: "none",
              background: saving || loading ? "#a5b4fc" : "#4f46e5", color: "#fff",
              cursor: saving || loading ? "default" : "pointer", fontWeight: 600,
            }}>
            {saving ? "Saving…" : "Confirm placement"}
          </button>
        </div>
      </div>
    </div>
  );
}
