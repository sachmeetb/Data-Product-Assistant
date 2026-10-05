import { useCallback, useEffect, useState } from "react";
import api from "../api/client";

/**
 * The code_spec review surface for code-migration (cmig) projects. Backs the
 * reverse-engineered use-case spec that must be approved before forward-
 * engineering runs. The spec lives in SQLite (not Neo4j), so this panel talks to
 * the /code-migration/spec endpoints directly (GET / PUT / approve / reopen).
 * Approving pins the spec hash server-side and rebuilds the :USES_DATASET edges;
 * editing re-opens review and voids any prior approval.
 */
export default function CodeSpecReviewPanel({
  projectId,
  onReviewComplete,
}: {
  projectId: number;
  onReviewComplete?: () => void;
}) {
  const [specText, setSpecText] = useState("");
  const [approved, setApproved] = useState(false);
  const [status, setStatus] = useState("");
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await api.get(`/api/projects/${projectId}/code-migration/spec`);
      setSpecText(JSON.stringify(res.data?.spec ?? {}, null, 2));
      setApproved(!!res.data?.approved);
      setStatus(res.data?.status ?? "");
      setDirty(false);
    } catch {
      setError("Could not load the code spec.");
    }
  }, [projectId]);

  useEffect(() => { load(); }, [load]);

  const save = async () => {
    setBusy(true); setError(null);
    try {
      const parsed = JSON.parse(specText);
      await api.put(`/api/projects/${projectId}/code-migration/spec`, { spec: parsed });
      await load();
      onReviewComplete?.();
    } catch (e: unknown) {
      setError(e instanceof SyntaxError ? "The spec is not valid JSON." : "Save failed.");
    }
    setBusy(false);
  };

  const approve = async () => {
    setBusy(true); setError(null);
    try {
      await api.post(`/api/projects/${projectId}/code-migration/spec/approve`);
      await load();
      onReviewComplete?.();
    } catch {
      setError("Approve failed — is the spec present?");
    }
    setBusy(false);
  };

  const reopen = async () => {
    setBusy(true); setError(null);
    try {
      await api.post(`/api/projects/${projectId}/code-migration/spec/reopen`);
      await load();
      onReviewComplete?.();
    } catch {
      setError("Reopen failed.");
    }
    setBusy(false);
  };

  return (
    <div style={{ padding: 20, backgroundColor: "#fff", borderRadius: 8, border: "1px solid #e2e8f0" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
        <div style={{ fontWeight: 600, color: "#334155" }}>Reverse-Engineered Spec Review</div>
        <span style={{
          fontSize: 12, padding: "2px 8px", borderRadius: 12,
          backgroundColor: approved ? "#dcfce7" : "#fef3c7",
          color: approved ? "#166534" : "#92400e",
        }}>
          {approved ? "approved" : status || "awaiting review"}
        </span>
      </div>
      <div style={{ fontSize: 13, color: "#64748b", marginBottom: 10 }}>
        Review the use-case spec the reverse-engineer stage produced. Edit if needed, then
        <strong> Approve</strong> to unblock forward-engineering. Editing an approved spec re-opens
        review and voids the approval.
      </div>
      {error && (
        <div style={{ padding: "8px 12px", backgroundColor: "#fee2e2", color: "#991b1b", borderRadius: 6, fontSize: 13, marginBottom: 10 }}>
          {error}
        </div>
      )}
      <textarea
        value={specText}
        onChange={(e) => { setSpecText(e.target.value); setDirty(true); }}
        spellCheck={false}
        style={{
          width: "100%", minHeight: 320, fontFamily: "monospace", fontSize: 12,
          padding: 10, border: "1px solid #cbd5e1", borderRadius: 6, boxSizing: "border-box",
        }}
      />
      <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
        <button onClick={save} disabled={busy || !dirty}
          style={{ padding: "6px 14px", borderRadius: 6, border: "1px solid #cbd5e1", background: "#f8fafc", cursor: busy || !dirty ? "default" : "pointer" }}>
          Save edits
        </button>
        {!approved ? (
          <button onClick={approve} disabled={busy || dirty}
            style={{ padding: "6px 14px", borderRadius: 6, border: "none", background: "#16a34a", color: "#fff", cursor: busy || dirty ? "default" : "pointer" }}>
            Approve spec
          </button>
        ) : (
          <button onClick={reopen} disabled={busy}
            style={{ padding: "6px 14px", borderRadius: 6, border: "1px solid #cbd5e1", background: "#fff", cursor: busy ? "default" : "pointer" }}>
            Reopen for edits
          </button>
        )}
      </div>
    </div>
  );
}
