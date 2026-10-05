import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import api from "../../api/client";
import DiscoveryView, { type SendToIntakeBody } from "../../components/DiscoveryView";
import type { ClusterProposal, ProjectInfo } from "../../types";

/**
 * Product-side estate discovery lens (moved off the engineering side).
 *
 * A Data Product Owner uses this standalone view to explore the estate's
 * object lineage + disposition decisions and design/determine data products
 * from it. The estate itself is global-fixture-backed (playbook/discovery/*),
 * so any `dmig` project acts as the container — we resolve the most recent one.
 *
 * Action routing (per the PO ↔ engineer split):
 *   - Modernize → the Product workbench's "Modernization intake" (PO owns product creation).
 *   - Migrate → the Engineer workbench's "Migration intake" (engineer owns migration).
 *   Both go through the intake bridge; the PO chooses to review the parsed form
 *   now or stage it and stay.
 *   - Retire → analysed in-place (impact preview only); it does not create a task.
 *   - Remain → no action; it is out of scope by definition.
 */
export default function DiscoveryPage() {
  const navigate = useNavigate();
  const [project, setProject] = useState<ProjectInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const [handoff, setHandoff] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const res = await api.get("/api/projects");
        const list: ProjectInfo[] = Array.isArray(res.data) ? res.data : (res.data.projects || []);
        const dmig = list
          .filter((p) => p.archetype === "dmig")
          .sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""));
        setProject(dmig[0] ?? null);
      } catch {
        setProject(null);
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  const containerId = project?.id;

  // Create / propose a data product from a cluster → PO product wizard, prefilled.
  const handleProposeCluster = async (ids: string[]): Promise<ClusterProposal> => {
    const res = await api.post(`/api/projects/${containerId}/discovery/propose-product-from-cluster`, { object_ids: ids });
    return res.data as ClusterProposal;
  };

  // Send-to-intake choice: the staged submission body + whether to review now.
  const [intakeChoice, setIntakeChoice] = useState<{ rowId: string; body: SendToIntakeBody } | null>(null);

  const [submitting, setSubmitting] = useState(false);

  // Send-to-intake (migrate + modernize). Both stage the node/cluster context
  // through the to-intake bridge; the worker parses it into a blueprint. The PO
  // then chooses to review the parsed form now or stage it and stay on Discovery.
  // Routing follows main's shell split: modernize → Product "Modernization
  // intake"; migrate → Engineer "Migration intake".
  const sendToIntake = (rowId: string, body: SendToIntakeBody) => setIntakeChoice({ rowId, body });

  const submitIntake = async (review: boolean) => {
    if (!containerId || !intakeChoice) return;
    const { rowId, body } = intakeChoice;
    const modernize = body.disposition === "modernize";
    const base = modernize ? "/product/intake" : "/engineer/intake";
    const label = modernize ? "Modernization" : "Migration";
    setSubmitting(true);
    try {
      const res = await api.post(`/api/projects/${containerId}/discovery/${rowId}/to-intake`, body);
      const intakeId = res.data?.intake_id;
      if (review && intakeId) navigate(`${base}/${intakeId}`);
      else setHandoff(`Sent to ${label} intake${intakeId ? ` (#${intakeId})` : ""} — review it any time from the ${modernize ? "Product" : "Engineer"} workbench's Intake.`);
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } } };
      setHandoff(`Could not send to intake: ${err.response?.data?.detail || "unexpected error"}.`);
    } finally {
      setSubmitting(false);
      setIntakeChoice(null);
    }
  };

  const header = useMemo(() => (
    <div style={{ marginBottom: 18 }}>
      <h1 style={{ fontSize: 22, fontWeight: 700, margin: 0 }}>Discovery</h1>
      <p style={{ color: "#64748b", fontSize: 14, margin: "4px 0 0" }}>
        Explore the estate's object lineage and disposition decisions to design and determine data products.
      </p>
    </div>
  ), []);

  if (loading) {
    return <div style={{ padding: 24 }}>{header}<div style={{ color: "#64748b" }}>Loading estate…</div></div>;
  }
  if (!project) {
    return (
      <div style={{ padding: 24 }}>
        {header}
        <div style={{ padding: "32px 20px", backgroundColor: "#f8fafc", border: "1px dashed #cbd5e1", borderRadius: 10, color: "#64748b", fontSize: 14, textAlign: "center" }}>
          No estate is available yet. Once an estate has been imported, its
          object lineage and disposition view will appear here.
        </div>
      </div>
    );
  }

  return (
    <div style={{ padding: 24 }}>
      {header}
      {handoff && (
        <div style={{ margin: "0 0 14px", padding: "9px 13px", backgroundColor: M.bg, border: `1px solid ${M.border}`, borderRadius: 8, fontSize: 13, color: M.deep, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <span>✓ {handoff}</span>
          <button onClick={() => setHandoff(null)} style={{ border: "none", background: "none", color: M.deep, cursor: "pointer", fontWeight: 600 }}>Dismiss</button>
        </div>
      )}
      <DiscoveryView
        project={project}
        onProposeProduct={handleProposeCluster}
        onSendToIntake={sendToIntake}
      />

      {intakeChoice && (() => {
        const modernize = intakeChoice.body.disposition === "modernize";
        const label = modernize ? "Modernization" : "Migration";
        const shell = modernize ? "Product" : "Engineer";
        return (
        <div
          style={{ position: "fixed", inset: 0, background: "rgba(15,23,42,0.45)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 1000 }}
          onClick={() => !submitting && setIntakeChoice(null)}
        >
          <div onClick={(e) => e.stopPropagation()} style={{ background: "#fff", borderRadius: 12, padding: 22, width: 480, maxWidth: "92vw", boxShadow: "0 20px 60px rgba(0,0,0,0.25)" }}>
            <h3 style={{ margin: "0 0 4px", fontSize: 16, fontWeight: 700 }}>Send to {label} intake</h3>
            <p style={{ margin: "0 0 16px", fontSize: 13, color: "#64748b" }}>
              <b>{intakeChoice.body.title}</b> will be staged in the <b>{shell} workbench</b>’s <b>{label} intake</b>, where its blueprint is parsed for review. Review the parsed contents now, or stage it and keep working.
            </p>
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              <button onClick={() => submitIntake(true)} disabled={submitting} style={{ textAlign: "left", padding: "12px 14px", borderRadius: 10, border: "1px solid #c7d2fe", background: "#eef2ff", cursor: submitting ? "default" : "pointer" }}>
                <div style={{ fontSize: 14, fontWeight: 700, color: "#3730a3" }}>Review in intake now</div>
                <div style={{ fontSize: 12, color: "#4338ca" }}>Open the {label.toLowerCase()} intake form and review the parsed contents.</div>
              </button>
              <button onClick={() => submitIntake(false)} disabled={submitting} style={{ textAlign: "left", padding: "12px 14px", borderRadius: 10, border: "1px solid #cbd5e1", background: "#fff", cursor: submitting ? "default" : "pointer" }}>
                <div style={{ fontSize: 14, fontWeight: 700, color: "#334155" }}>Send &amp; stay here</div>
                <div style={{ fontSize: 12, color: "#64748b" }}>Stage it and keep working in Discovery; review it later from Intake.</div>
              </button>
            </div>
            <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 14 }}>
              <button onClick={() => setIntakeChoice(null)} disabled={submitting} style={{ padding: "8px 14px", borderRadius: 8, border: "1px solid #cbd5e1", background: "#fff", color: "#475569", fontSize: 13, fontWeight: 600, cursor: "pointer" }}>
                {submitting ? "Sending…" : "Cancel"}
              </button>
            </div>
          </div>
        </div>
        );
      })()}

    </div>
  );
}
