import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import api from "../../api/client";
import SourceProductValidationPanel from "../../components/SourceProductValidationPanel";
import { productTheme } from "../../theme";

interface ProjectInfo {
  id: number;
  name: string;
  domain: string | null;
  product_idea: string | null;
  archetype: string;
}

interface ContractInfo {
  lifecycle_state: string | null;
}

/**
 * PO-only landing page for the dpe-sa po_source_validation review.
 * Reached from MyProductsDashboard's "Validate now" button on an in-flight
 * SA card. Mounts the same SourceProductValidationPanel the engineer's
 * Reviews tab uses; the backend auto-flips the po_source_validation stage
 * to complete once all three buckets are empty (see reviews.py
 * _check_review_complete), unblocking the engineer's materialization stage.
 */
export default function PoValidationPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const navigate = useNavigate();
  const theme = productTheme;
  const pid = projectId ? parseInt(projectId) : null;
  const [project, setProject] = useState<ProjectInfo | null>(null);
  const [contract, setContract] = useState<ContractInfo | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (pid === null || isNaN(pid)) { setError("Invalid project id"); return; }
    api.get<ProjectInfo>(`/api/projects/${pid}`)
      .then((r) => setProject(r.data))
      .catch((e) => setError(String(e)));
    // Edit-mode discriminator: if the project's contract is published or
    // approved, the PO is editing an already-in-flight product. The panel
    // surfaces approved items with a Re-edit affordance instead of just
    // pending-review items. 404 here is expected (no contract yet — the
    // initial validation gate run hasn't materialized one).
    api.get<ContractInfo>(`/api/projects/${pid}/odcs`)
      .then((r) => setContract(r.data))
      .catch(() => setContract(null));
  }, [pid]);

  const editMode = (contract?.lifecycle_state === "published" || contract?.lifecycle_state === "approved");

  if (pid === null || isNaN(pid)) {
    return <div style={{ color: "#dc2626" }}>Invalid project id.</div>;
  }
  if (error) {
    return <div style={{ color: "#dc2626" }}>{error}</div>;
  }
  if (!project) {
    return <div style={{ color: "#64748b" }}>Loading...</div>;
  }

  return (
    <div style={{ maxWidth: 1080, margin: "0 auto" }}>
      <div style={{ marginBottom: 16 }}>
        <Link to="/product/my-products" style={{ fontSize: 13, color: theme.accent, textDecoration: "none" }}>
          ← Back to My Products
        </Link>
      </div>

      <div style={{ marginBottom: 16 }}>
        <div style={{ fontSize: 11, color: theme.accent, textTransform: "uppercase", letterSpacing: 0.4, fontWeight: 700 }}>
          {editMode ? "Edit source product" : "Validate source product"}
        </div>
        <h1 style={{ fontSize: 22, fontWeight: 700, color: "#0f172a", margin: "4px 0 6px" }}>{project.name}</h1>
        <div style={{ fontSize: 13, color: "#64748b" }}>
          {project.domain ? <>Domain: <strong style={{ color: "#334155" }}>{project.domain}</strong>. </> : null}
          {editMode
            ? "All names, descriptions, and rules are listed below. Click Re-edit on an approved item to flip it back to pending and change it. Engineering reruns materialization once you're done — that creates a new contract version."
            : "Approve, edit, or reject the engineer's recommendations across names, descriptions, and rules. When you're done with all three tabs, the engineer can materialize the contract."}
        </div>
      </div>

      {project.product_idea && (
        <div
          style={{
            marginBottom: 16,
            padding: 12,
            borderRadius: 8,
            border: `1px solid ${theme.accentSoft}`,
            backgroundColor: theme.badgeBg,
            fontSize: 13,
            color: "#334155",
            whiteSpace: "pre-wrap",
          }}
        >
          <div style={{ fontSize: 11, fontWeight: 600, color: theme.accent, marginBottom: 4, textTransform: "uppercase", letterSpacing: 0.4 }}>
            Your original intent
          </div>
          {project.product_idea}
        </div>
      )}

      <div
        style={{
          padding: 16,
          borderRadius: 10,
          border: "1px solid #e2e8f0",
          backgroundColor: "#fff",
        }}
      >
        <SourceProductValidationPanel
          projectId={pid}
          editMode={editMode}
          onReviewComplete={() => {
            // Backend already flipped the stage to complete; the engineer
            // can run materialization. Bounce the PO back to the dashboard.
            navigate("/product/my-products");
          }}
        />
      </div>
    </div>
  );
}
