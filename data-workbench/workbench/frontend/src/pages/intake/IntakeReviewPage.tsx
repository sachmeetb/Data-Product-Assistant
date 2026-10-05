/**
 * Review one intake submission and approve it into scaffolded project(s).
 *
 * Autosave CONFIRMS success (it does not swallow failures, unlike the ingest
 * page): a dirty or failed save blocks Approve. Approve sends expected_revision
 * (optimistic concurrency) and is disabled until every gap is resolved.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import api from "../../api/client";
import { engineerTheme, productTheme } from "../../theme";
import BlueprintReview from "./BlueprintReview";
import { usePrompt } from "../../components/dialogContext";
import type { Blueprint, IntakeSubmission, MigrationBlueprint, ModernizationBlueprint, ProductCandidate } from "./types";

function errText(e: unknown, fallback: string): string {
  const err = e as { response?: { data?: { detail?: unknown; message?: unknown } } };
  const detail = err?.response?.data?.detail ?? err?.response?.data?.message;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object") {
    const d = detail as { message?: string; blockers?: string[] };
    if (d.message) return d.blockers ? `${d.message}: ${d.blockers.join(", ")}` : d.message;
    return JSON.stringify(detail);
  }
  return fallback;
}

function localBlockers(bp: Blueprint | null): string[] {
  if (!bp) return ["no blueprint yet"];
  const out: string[] = [];
  if (bp.gaps && bp.gaps.length) out.push(`${bp.gaps.length} unresolved gap(s)`);
  if (bp.scenario === "migration") {
    if (!(bp as MigrationBlueprint).project_name?.value) out.push("project name is missing");
  } else {
    const m = bp as ModernizationBlueprint;
    const included = (arr?: ProductCandidate[]) => (arr || []).filter((c) => c.review_state !== "excluded").length;
    if (included(m.source_aligned) + included(m.consumer_aligned) === 0) out.push("no product candidates included");
  }
  return out;
}

export default function IntakeReviewPage() {
  const { id } = useParams<{ id: string }>();
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const isProduct = pathname.startsWith("/product");
  const basePath = isProduct ? "/product" : "/engineer";
  const accent = (isProduct ? productTheme : engineerTheme).accent;

  const prompt = usePrompt();
  const [sub, setSub] = useState<IntakeSubmission | null>(null);
  const [bp, setBp] = useState<Blueprint | null>(null);
  const [revision, setRevision] = useState(0);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<Record<string, unknown> | null>(null);

  const bpRef = useRef<Blueprint | null>(null);
  const revRef = useRef(0);
  const timer = useRef<number | undefined>(undefined);

  const load = useCallback(async () => {
    const res = await api.get(`/api/intake/${id}`);
    const s = res.data as IntakeSubmission;
    setSub(s);
    setBp(s.blueprint);
    bpRef.current = s.blueprint;
    setRevision(s.blueprint_revision);
    revRef.current = s.blueprint_revision;
    setDirty(false);
    setSaveError(null);
  }, [id]);

  useEffect(() => {
    load().catch(() => setActionError("Failed to load submission."));
  }, [load]);

  // Poll while the worker is still parsing.
  useEffect(() => {
    if (!sub || (sub.status !== "received" && sub.status !== "parsing")) return;
    const t = window.setInterval(() => load().catch(() => {}), 3000);
    return () => window.clearInterval(t);
  }, [sub, load]);

  const doSave = useCallback(async () => {
    if (!bpRef.current) return;
    setSaving(true);
    try {
      const res = await api.patch(`/api/intake/${id}/blueprint`, {
        expected_revision: revRef.current,
        blueprint: bpRef.current,
      });
      const rev = res.data.blueprint_revision as number;
      setRevision(rev);
      revRef.current = rev;
      setDirty(false);
      setSaveError(null);
    } catch (e: unknown) {
      const status = (e as { response?: { status?: number } })?.response?.status;
      if (status === 409) {
        // stale revision — reload authoritative state
        await load().catch(() => {});
        setSaveError("Reloaded — your edit conflicted with a newer revision; re-apply it.");
      } else {
        setSaveError(errText(e, "Save failed."));
      }
    } finally {
      setSaving(false);
    }
  }, [id, load]);

  const handleChange = (next: Blueprint) => {
    setBp(next);
    bpRef.current = next;
    setDirty(true);
    setSaveError(null);
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => void doSave(), 700);
  };

  const approve = async () => {
    setBusy(true);
    setActionError(null);
    try {
      const res = await api.post(`/api/intake/${id}/approve`, { expected_revision: revRef.current });
      setResult((res.data?.result as Record<string, unknown>) || { status: res.data?.status });
      await load().catch(() => {});
    } catch (e: unknown) {
      setActionError(errText(e, "Approve failed."));
      await load().catch(() => {});
    } finally {
      setBusy(false);
    }
  };

  const reject = async () => {
    const reason = (await prompt({
      title: "Reject submission",
      label: "Reason (optional)",
      confirmLabel: "Reject",
      multiline: true,
    })) ?? undefined;
    if (reason === undefined) return; // cancelled
    setBusy(true);
    setActionError(null);
    try {
      await api.post(`/api/intake/${id}/reject`, { reason });
      navigate(`${basePath}/intake`);
    } catch (e: unknown) {
      setActionError(errText(e, "Reject failed."));
    } finally {
      setBusy(false);
    }
  };

  const setExecutionMode = async (mode: "live" | "schema_only") => {
    setBusy(true);
    setActionError(null);
    try {
      await api.patch(`/api/intake/${id}/execution-mode`, { execution_mode: mode });
      await load().catch(() => {});
    } catch (e: unknown) {
      setActionError(errText(e, "Could not change execution mode."));
    } finally {
      setBusy(false);
    }
  };

  const reparse = async () => {
    setBusy(true);
    try {
      await api.post(`/api/intake/${id}/reparse`, {});
      await load().catch(() => {});
    } finally {
      setBusy(false);
    }
  };

  if (!sub) {
    return <div style={{ padding: 40, color: "#64748b" }}>{actionError || "Loading…"}</div>;
  }

  const editable = sub.status === "proposed" || sub.status === "reviewing";
  const parsing = sub.status === "received" || sub.status === "parsing";
  const blockers = localBlockers(bp);
  const canApprove = editable && !dirty && !saving && !saveError && blockers.length === 0 && !busy;

  return (
    <div style={{ maxWidth: 860, margin: "0 auto", padding: "24px 20px" }}>
      <button onClick={() => navigate(`${basePath}/intake`)} style={{ background: "none", border: "none", color: accent, cursor: "pointer", fontSize: 13, padding: 0, marginBottom: 8 }}>
        ← Back to intake queue
      </button>
      <h1 style={{ fontSize: 20, margin: "0 0 2px" }}>
        Review intake · {sub.source_system}
      </h1>
      <div style={{ color: "#94a3b8", fontSize: 12, marginBottom: 16 }}>
        {sub.external_ref} · status <strong>{sub.status.replace(/_/g, " ")}</strong> · rev {revision}
      </div>

      {(sub as any).ingestion_mode === "structured" && sub.status !== "scaffolded" && (
        <div style={{ border: "1px solid #ddd6fe", background: "#f5f3ff", borderRadius: 10, padding: "10px 14px", marginBottom: 14, color: "#5b21b6", fontSize: 13 }}>
          <strong>Authored in the Product Assembly workspace</strong> — this portfolio was composed from a
          feasibility evaluation, not an unstructured parse. The decisions are already made; this is a quick
          <strong> confirm</strong>. Review the source products + the aggregate below, then Approve to scaffold.
        </div>
      )}

      {parsing && (
        <div style={{ border: "1px solid #c7d2fe", background: "#eef2ff", borderRadius: 10, padding: 16, color: "#4338ca" }}>
          Parsing the submission… this refreshes automatically.
        </div>
      )}

      {sub.status === "parse_failed" && (
        <div style={{ border: "1px solid #fecaca", background: "#fef2f2", borderRadius: 10, padding: 16 }}>
          <div style={{ fontWeight: 600, color: "#991b1b" }}>Parsing failed</div>
          <div style={{ fontSize: 13, color: "#b91c1c", marginTop: 4 }}>{sub.parse_meta?.error || "The parser could not produce a valid blueprint."}</div>
          <button onClick={reparse} disabled={busy} style={{ marginTop: 10, padding: "6px 12px", borderRadius: 6, border: "1px solid #fca5a5", background: "#fff", color: "#991b1b", cursor: "pointer" }}>
            Re-parse
          </button>
        </div>
      )}

      {sub.status === "scaffolded" && (
        <div style={{ border: "1px solid #bbf7d0", background: "#dcfce7", borderRadius: 10, padding: 16, marginBottom: 16 }}>
          <div style={{ fontWeight: 600, color: "#166534" }}>Scaffolded ✓</div>
          <pre style={{ fontSize: 12, color: "#166534", whiteSpace: "pre-wrap", margin: "6px 0 0" }}>{JSON.stringify(result || {}, null, 2)}</pre>
        </div>
      )}

      {sub.platform_advisories && sub.platform_advisories.length > 0 && (
        <div style={{ margin: "12px 0", display: "flex", flexDirection: "column", gap: 6 }}>
          {sub.platform_advisories.map((a, i) => {
            const palette =
              a.severity === "error"
                ? { bg: "#fffbeb", border: "#fcd34d", fg: "#92400e", icon: "⚠" }
                : { bg: "#ecfdf5", border: "#a7f3d0", fg: "#065f46", icon: "✓" };
            return (
              <div key={i} style={{ border: `1px solid ${palette.border}`, background: palette.bg, color: palette.fg, borderRadius: 8, padding: "8px 12px", fontSize: 13, display: "flex", gap: 8, alignItems: "baseline" }}>
                <span style={{ fontWeight: 700 }}>{palette.icon}</span>
                <span>{a.message}</span>
              </div>
            );
          })}
        </div>
      )}

      {bp && bp.scenario === "migration" && (
        <div style={{ margin: "12px 0", border: "1px solid #e2e8f0", background: "#fff", borderRadius: 10, padding: "10px 14px" }}>
          <div style={{ fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: 0.4, color: "#64748b", marginBottom: 6 }}>
            Source connectivity
          </div>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            {(["live", "schema_only"] as const).map((mode) => {
              const active = (sub.execution_mode || "live") === mode;
              return (
                <button
                  key={mode}
                  onClick={() => !active && editable && setExecutionMode(mode)}
                  disabled={!editable || busy}
                  style={{ padding: "6px 12px", borderRadius: 8, fontSize: 13, cursor: editable && !active ? "pointer" : "default", border: `1px solid ${active ? accent : "#cbd5e1"}`, background: active ? accent : "#fff", color: active ? "#fff" : "#334155", fontWeight: active ? 600 : 400 }}
                >
                  {mode === "live" ? "Live source" : "Schema-only (offline)"}
                </button>
              );
            })}
          </div>
          <div style={{ fontSize: 12, color: "#64748b", marginTop: 6 }}>
            {(sub.execution_mode || "live") === "schema_only"
              ? "No live source yet — the project will materialize the assessment schema as its catalog and lock Run/Reconcile until connectivity arrives."
              : "A live source connection is expected; the engineer runs discovery against the real database."}
          </div>
        </div>
      )}

      {bp && (
        <>
          <div style={{ margin: "8px 0 16px" }}>
            <BlueprintReview blueprint={bp} onChange={handleChange} readOnly={!editable} accent={accent} />
          </div>

          {editable && (
            <div style={{ position: "sticky", bottom: 0, background: "var(--bg-tint, #f8fafc)", borderTop: "1px solid #e2e8f0", padding: "12px 0", display: "flex", alignItems: "center", gap: 12 }}>
              <span style={{ fontSize: 12, color: saveError ? "#991b1b" : dirty || saving ? "#854d0e" : "#166534" }}>
                {saving ? "Saving…" : saveError ? `Not saved — ${saveError}` : dirty ? "Unsaved edits…" : "All edits saved"}
              </span>
              <div style={{ flex: 1 }} />
              {blockers.length > 0 && (
                <span style={{ fontSize: 12, color: "#854d0e" }}>Resolve first: {blockers.join(", ")}</span>
              )}
              <button onClick={reject} disabled={busy} style={{ padding: "8px 14px", borderRadius: 8, border: "1px solid #cbd5e1", background: "#fff", color: "#334155", cursor: "pointer" }}>
                Reject
              </button>
              <button
                onClick={approve}
                disabled={!canApprove}
                title={canApprove ? "" : "Resolve gaps and save edits first"}
                style={{ padding: "8px 16px", borderRadius: 8, border: "none", background: canApprove ? accent : "#cbd5e1", color: "#fff", cursor: canApprove ? "pointer" : "not-allowed", fontWeight: 600 }}
              >
                {busy ? "Working…" : "Approve & Scaffold"}
              </button>
            </div>
          )}
        </>
      )}

      {actionError && <div style={{ color: "#991b1b", fontSize: 13, marginTop: 10 }}>{actionError}</div>}
    </div>
  );
}
