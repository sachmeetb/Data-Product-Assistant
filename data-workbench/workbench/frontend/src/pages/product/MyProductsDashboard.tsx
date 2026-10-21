import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import api from "../../api/client";
import { useCurrentUserEmail } from "../../AuthContext";
import { productTheme } from "../../theme";
import OsiBadge, { type OsiBand } from "../../components/OsiBadge";
import OsiAnalysisPanel from "../../components/OsiAnalysisPanel";
import ProductKindChip from "../../components/ProductKindChip";
import DeployChecklistModal from "../../components/DeployChecklistModal";
import { useConfirm, useNotify } from "../../components/dialogContext";
import type { IngestClassification, IngestDraftRow, ResolveSlot, SourceCandidateRequest } from "../../types";

interface Product {
  uri: string;
  name: string;
  status: string;
  lifecycle_state: string;
  contract_id: string | null;
  project_id: number | null;
  published_at: string | null;
  published_by: string | null;
  owner_name: string | null;
  owner_role: string | null;
  description: string | null;
  column_count: number;
  product_kind: string;
  // True when the head is an in-flight DRAFT version branched from a prior one
  // (currentVersion > 1) — i.e. an unintended edit that can be rolled back.
  discardable_draft?: boolean;
  osi_band: OsiBand;
  osi_completeness: number | null;
  osi_conformance_pass: boolean | null;
  osi_evaluated_at: string | null;
  scoring_rubric: string | null;
  rubric_short_label: string | null;
}

// In-flight SA requests — pre-contract, so they don't show up in the
// marketplace listing. Returned by GET /api/my-products/in-flight.
interface InFlightRequest {
  request_id: number;
  project_id: number;
  project_code: string;
  name: string;
  domain: string | null;
  product_idea: string | null;
  submitted_at: string | null;
  discovery_complete_at: string | null;
  status: string;
  status_label: string;
  ready_for_validation: boolean;
}

// Derived (consumer-aligned, dpe-cf) products still in DRAFT — scaffolded from
// intake (or saved mid-wizard) but not yet published. They have no ProductRequest
// and no marketplace listing, so this is their only PO surface. Returned by
// GET /api/my-products/derived-drafts.
interface DerivedDraft {
  project_id: number;
  project_code: string;
  name: string;
  domain: string | null;
  product_idea: string | null;
  lifecycle_state: string;
  from_intake: boolean;
  dependencies_total: number;
  dependencies_ready: number;
  // Count of deps whose :CONSUMES edge is already wired (flipped to 'bound' on a
  // consumer contract save). Distinct from dependencies_ready (source published).
  dependencies_bound: number;
  sources: { name: string; project_code: string | null; ready: boolean; bound?: boolean }[];
  created_at: string | null;
}

// `published` is editable: opening the wizard hydrates from the deployed
// contract; the *first save* triggers `_save_odcs_to_graph` to branch a new
// :DataContract version (v2 starts at lifecycleState='draft', v1 stays
// 'published' but isCurrent=false). Marketplace listings are pinned to the
// latest deployed version, so v1 stays live for consumers until v2 is
// deployed. See plan Phase C.
//
// `approved` is editable: same branching semantics as `published`. v1 stays
// at 'approved' isCurrent=false (a harmless orphan — never deployed, never
// picked up by the marketplace pin). The PO uses this when they change
// their mind after the engineer signed off but before deploying.
const EDITABLE_STATES = new Set(["draft", "submitted", "ingesting", "rejected", "approved", "published"]);

const LIFECYCLE_LABEL: Record<string, string> = {
  draft: "Draft",
  ingesting: "Ingesting",
  submitted: "Awaiting engineering",
  in_engineering: "In engineering",
  approved: "Ready to deploy",
  published: "Deployed",
  superseded: "Superseded",
  rejected: "Returned — needs revision",
};

const LIFECYCLE_COLORS: Record<string, { bg: string; fg: string }> = {
  draft: { bg: "#f1f5f9", fg: "#475569" },
  ingesting: { bg: "#fef3c7", fg: "#92400e" },
  submitted: { bg: "#dbeafe", fg: "#1d4ed8" },
  in_engineering: { bg: "#fde68a", fg: "#92400e" },
  approved: { bg: "#ede9fe", fg: "#5b21b6" },
  published: { bg: "#dcfce7", fg: "#065f46" },
  superseded: { bg: "#fee2e2", fg: "#991b1b" },
  rejected: { bg: "#fee2e2", fg: "#991b1b" },
};

// Phase 5: incoming consumer-pushback :ProductRequest rows. Surface as a
// dismissible banner so the source PO sees consumer feedback without
// digging into the engineer's queue.
interface IncomingPushback {
  request_id: number;
  source_project_id: number;
  source_project_code: string;
  source_project_name: string;
  contract_id: string;
  submitted_by: string;
  submitted_at: string | null;
  notes: string | null;
}

interface FeasibilityCandidate {
  id: number;
  score_id: number;
  run_id: number;
  estate_id: number;
  spec_id: string;
  spec_name: string;
  domain: string | null;
  tier: string;
  required_coverage: number;
  confidence: number;
  rationale: string;
  adaptation_notes: string;
  notes: string;
  status: string;
  saved_by: string;
  created_at: string | null;
}

const CANDIDATE_TIER: Record<string, { bg: string; fg: string; label: string }> = {
  ready:       { bg: "#10b981", fg: "#fff",     label: "Ready" },
  adaptable:   { bg: "#38bdf8", fg: "#0c4a6e",  label: "Adaptable" },
  assemblable: { bg: "#f59e0b", fg: "#fff",     label: "Assemblable" },
  absent:      { bg: "#94a3b8", fg: "#fff",     label: "Absent" },
};

export default function MyProductsDashboard() {
  const navigate = useNavigate();
  const CURRENT_USER_EMAIL = useCurrentUserEmail();
  const [products, setProducts] = useState<Product[]>([]);
  const [inFlight, setInFlight] = useState<InFlightRequest[]>([]);
  const [pushbacks, setPushbacks] = useState<IncomingPushback[]>([]);
  // Persisted in-flight ingest drafts owned by this PO. Shown above the SA
  // in-flight section so the PO can resume a half-finished consumer-aligned
  // ingest after authoring/importing the source products it depends on.
  const [ingestDrafts, setIngestDrafts] = useState<IngestDraftRow[]>([]);
  // Engineer→PO source-candidates-needed requests for this PO's consumer
  // products. Surfaced above pushbacks in the same orange-card style.
  const [sourceCandidateRequests, setSourceCandidateRequests] = useState<SourceCandidateRequest[]>([]);
  const [candidates, setCandidates] = useState<FeasibilityCandidate[]>([]);
  // Derived (dpe-cf) products still in draft — e.g. an aggregate scaffolded from
  // an estate→feasibility→intake portfolio. Their only PO surface (no request,
  // no marketplace listing until published).
  const [derivedDrafts, setDerivedDrafts] = useState<DerivedDraft[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Tracks which project_id is mid-deploy so we can disable the button and
  // surface a "Deploying..." label. Cleared on success or failure.
  const [deploying, setDeploying] = useState<number | null>(null);
  // Consumer product whose pre-deploy checklist modal is open (project_id).
  const [deployChecklistFor, setDeployChecklistFor] = useState<number | null>(null);
  // Tracks which project_id is mid-delete so we can disable the button and
  // suppress repeat-click while the backend tears everything down.
  const [deleting, setDeleting] = useState<number | null>(null);
  const [discarding, setDiscarding] = useState<number | null>(null);
  // Project_id whose analysis modal is currently open (null = closed).
  const [analysisFor, setAnalysisFor] = useState<{ projectId: number; productName: string } | null>(null);
  const confirm = useConfirm();
  const { showError, notify } = useNotify();

  const refreshList = () =>
    Promise.all([
      api.get(`/api/marketplace`, { params: { owned_by: CURRENT_USER_EMAIL } }),
      api.get(`/api/my-products/in-flight`, { params: { owner_email: CURRENT_USER_EMAIL } }),
      api.get(`/api/my-products/incoming-pushbacks`),
      api.get(`/api/ingest-products/drafts`, {
        params: { owner_email: CURRENT_USER_EMAIL, status: "in_progress" },
      }),
      api.get(`/api/my-products/source-candidate-requests`, {
        params: { owner_email: CURRENT_USER_EMAIL },
      }),
      api.get(`/api/feasibility/candidates`, {
        params: { owner_email: CURRENT_USER_EMAIL },
      }),
      api.get(`/api/my-products/derived-drafts`, {
        params: { owner_email: CURRENT_USER_EMAIL },
      }),
    ])
      .then(([mk, inf, pb, drafts, scr, cands, derived]) => {
        setProducts(mk.data.products || []);
        setInFlight(inf.data.requests || []);
        setPushbacks(pb.data.items || []);
        setIngestDrafts(drafts.data?.drafts || []);
        setSourceCandidateRequests(scr.data?.items || []);
        setCandidates(cands.data?.candidates || []);
        setDerivedDrafts(derived.data?.drafts || []);
        setError(null);
      })
      .catch((e) => setError(String(e)));

  const resolvePushback = async (requestId: number, action: "accept" | "dismiss") => {
    try {
      await api.post(`/api/my-products/incoming-pushbacks/resolve`, {
        request_id: requestId,
        action,
      });
      await refreshList();
    } catch (e) {
      setError(String(e));
    }
  };

  /** Acknowledge or dismiss an engineer's source-candidates-needed request.
   *  Acknowledge → flips the request to 'accepted' and deep-links the PO
   *  into the consumer wizard at step 8 (Confirm candidate sources). The
   *  wizard's saveDraft path flips it to 'complete' once new candidates
   *  land in the spec. Dismiss → flips to 'rejected'. */
  const resolveSourceCandidateRequest = async (
    request: SourceCandidateRequest,
    action: "accept" | "dismiss",
  ) => {
    try {
      await api.post(`/api/my-products/source-candidate-requests/resolve`, {
        request_id: request.request_id,
        action,
      });
      if (action === "accept") {
        // Route the PO directly into the consumer wizard's Confirm step.
        // The wizard mounts on the `edit/:projectId` route (there is no
        // `new/consumer/:projectId` route — a param path falls through to home)
        // and reads ?step / ?from_request as route-independent query params.
        navigate(`/product/edit/${request.project_id}?step=9&from_request=${request.request_id}`);
      } else {
        await refreshList();
      }
    } catch (e) {
      setError(String(e));
    }
  };

  const dismissCandidate = async (candidate: FeasibilityCandidate) => {
    try {
      await api.delete(`/api/feasibility/scores/${candidate.score_id}/candidate`);
      setCandidates((prev) => prev.filter((c) => c.id !== candidate.id));
    } catch (e) {
      setError(String(e));
    }
  };

  useEffect(() => {
    setLoading(true);
    refreshList().finally(() => setLoading(false));
  }, []);

  // Owner-initiated teardown. First call omits ?force=true — if the product
  // is consumed by other projects the backend returns 409 with the consumer
  // list; we surface them and retry on confirm.
  const handleDelete = async (projectId: number, productName: string) => {
    if (deleting !== null) return;
    const ok = await confirm({
      title: `Delete "${productName}"`,
      message: "This permanently removes the project, its knowledge-graph nodes, and its on-disk folder. Cannot be undone.",
      confirmLabel: "Delete",
      tone: "danger",
    });
    if (!ok) return;
    setDeleting(projectId);
    const callDelete = async (force: boolean) =>
      api.delete(`/api/projects/${projectId}`, {
        params: { owner_email: CURRENT_USER_EMAIL, ...(force ? { force: true } : {}) },
      });
    try {
      try {
        await callDelete(false);
      } catch (err) {
        const resp = (err as { response?: { status?: number; data?: { detail?: unknown } } }).response;
        const detail = resp?.data?.detail;
        const isConsumerBlock =
          resp?.status === 409 &&
          typeof detail === "object" &&
          detail !== null &&
          (detail as { reason?: string }).reason === "consumers_exist";
        if (!isConsumerBlock) throw err;
        const consumers = ((detail as { consumers?: Array<{ name?: string; contract_id?: string; lifecycle_state?: string }> }).consumers) || [];
        const lines = consumers
          .map((c) => `• ${c.name || c.contract_id || "(unnamed)"}${c.lifecycle_state ? ` — ${c.lifecycle_state}` : ""}`)
          .join("\n");
        const proceed = await confirm({
          title: "Product is consumed by others",
          message: (
            <>
              This product is consumed by {consumers.length} other product(s):
              <pre style={{ whiteSpace: "pre-wrap", margin: "8px 0", fontFamily: "inherit" }}>{lines}</pre>
              Deleting it will leave stale references in those products. Still delete?
            </>
          ),
          confirmLabel: "Delete anyway",
          tone: "danger",
        });
        if (!proceed) {
          setDeleting(null);
          return;
        }
        await callDelete(true);
      }
      await refreshList();
    } catch (e) {
      const msg = (e as { response?: { data?: { detail?: unknown } }; message?: string }).response?.data?.detail
        ?? (e as Error).message
        ?? "Delete failed";
      setError(typeof msg === "string" ? msg : "Delete failed");
    } finally {
      setDeleting(null);
    }
  };

  // Discard an unintended draft version (e.g. one created by accidentally
  // walking the wizard on a published product) and roll back to the prior
  // version. Backend guardrails this to draft branches only.
  const handleDiscardDraft = async (projectId: number, productName: string) => {
    if (discarding !== null) return;
    const ok = await confirm({
      title: `Discard draft version of "${productName}"`,
      message:
        "Roll back to the previous version? This deletes the in-flight draft (and any columns it added) and restores the prior " +
        "version as the current one. Existing column mappings are preserved. Cannot be undone.",
      confirmLabel: "Discard draft",
      tone: "danger",
    });
    if (!ok) return;
    setDiscarding(projectId);
    try {
      const res = await api.post(`/api/projects/${projectId}/odcs/discard-draft`);
      if (res.data?.warning) await notify({ tone: "warning", title: "Draft discarded", message: res.data.warning });
      await refreshList();
    } catch (e) {
      const msg = (e as { response?: { data?: { detail?: unknown } }; message?: string }).response?.data?.detail
        ?? (e as Error).message
        ?? "Discard failed";
      setError(typeof msg === "string" ? msg : "Discard failed");
    } finally {
      setDiscarding(null);
    }
  };

  // Deploy gesture from the My Products card. Same endpoint the marketplace
  // detail page uses; just lifts the action up so the PO doesn't have to
  // navigate to deploy.
  const deploy = async (projectId: number) => {
    if (deploying !== null) return;
    setDeploying(projectId);
    try {
      await api.post(`/api/projects/${projectId}/odcs/publish`);
      setDeployChecklistFor(null);
      await refreshList();
    } catch (e) {
      showError(e, { title: "Deploy failed" });
    }
    setDeploying(null);
  };

  return (
    <div style={{ maxWidth: 960, margin: "0 auto" }}>
      {deployChecklistFor !== null && (
        <DeployChecklistModal
          projectId={deployChecklistFor}
          deploying={deploying === deployChecklistFor}
          onDeploy={() => deploy(deployChecklistFor)}
          onEditDetails={() =>
            // Edit-mode wizard (the `edit/:projectId` route); ?step is a
            // route-independent query param. There is no `new/consumer/:id` route.
            navigate(`/product/edit/${deployChecklistFor}?step=6`)
          }
          onClose={() => setDeployChecklistFor(null)}
        />
      )}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 16 }}>
        <div>
          <h1 style={{ fontSize: 22, fontWeight: 700, color: "#0f172a", margin: 0 }}>My Products</h1>
          <div style={{ fontSize: 13, color: "#64748b", marginTop: 4 }}>
            Products where you're listed as an owner ({CURRENT_USER_EMAIL})
          </div>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <Link to="/product/new/source" style={ctaPrimary} title="Engineer profiles a source DB; you validate names and rules.">
            + Source-aligned
          </Link>
          <Link to="/product/new/consumer" style={ctaSecondary} title="Compose a product from already-published products (source, aggregate, or consumer).">
            + Derived product
          </Link>
          <Link to="/product/ingest" style={ctaTertiary} title="Already have an ODCS YAML? Register it directly.">
            Register existing
          </Link>
        </div>
      </div>

      {loading && <div style={{ color: "#64748b" }}>Loading...</div>}
      {error && (
        <div style={{ color: "#dc2626", backgroundColor: "#fef2f2", padding: 12, borderRadius: 6 }}>{error}</div>
      )}
      {!loading && !error && products.length === 0 && inFlight.length === 0 && derivedDrafts.length === 0 && ingestDrafts.length === 0 && (
        <div
          style={{
            padding: 32,
            textAlign: "center",
            border: "1px dashed #cbd5e1",
            borderRadius: 10,
            color: "#64748b",
          }}
        >
          You don't have any products in flight or published. Propose one to get started.
        </div>
      )}

      {/* Engineer→PO source-candidates-needed requests. Rendered above
          consumer pushbacks because they actively block engineering work —
          mapping can't proceed without the PO's input. Same orange-card
          pattern; Acknowledge deep-links to the consumer wizard's Confirm
          candidate sources step (step 8) in edit mode. */}
      {sourceCandidateRequests.length > 0 && (
        <div style={{ marginBottom: 16 }}>
          <div style={{ fontSize: 12, color: "#9a3412", textTransform: "uppercase", letterSpacing: 0.4, fontWeight: 600, marginBottom: 8 }}>
            Engineer needs source candidates ({sourceCandidateRequests.length})
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {sourceCandidateRequests.map((r) => (
              <div
                key={r.request_id}
                style={{
                  padding: 12,
                  background: "#fff7ed",
                  border: "1px solid #fdba74",
                  borderRadius: 8,
                  display: "flex",
                  gap: 12,
                  alignItems: "flex-start",
                }}
              >
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap", marginBottom: 4 }}>
                    <strong style={{ fontSize: 13, color: "#9a3412" }}>
                      Source candidates needed for {r.project_name}
                    </strong>
                    <span style={{ fontSize: 11, color: "#92400e" }}>
                      from {r.engineer}
                    </span>
                  </div>
                  {r.notes && (
                    <div style={{ fontSize: 13, color: "#374151", whiteSpace: "pre-wrap" }}>
                      {r.notes}
                    </div>
                  )}
                  {r.gap_column_uri && (
                    <div style={{ fontSize: 11, color: "#78350f", marginTop: 4, fontFamily: "monospace" }}>
                      Gap: {r.gap_column_uri}
                    </div>
                  )}
                  {r.gap_reason && (
                    <div style={{ fontSize: 12, color: "#374151", marginTop: 4, fontStyle: "italic" }}>
                      "{r.gap_reason}"
                    </div>
                  )}
                </div>
                <div style={{ display: "flex", gap: 6 }}>
                  <button
                    onClick={() => resolveSourceCandidateRequest(r, "accept")}
                    style={{
                      padding: "4px 10px",
                      background: "#16a34a",
                      color: "white",
                      border: "none",
                      borderRadius: 4,
                      fontSize: 12,
                      fontWeight: 600,
                      cursor: "pointer",
                    }}
                    title="Acknowledge and open the wizard at the candidate-sources step"
                  >
                    Acknowledge
                  </button>
                  <button
                    onClick={() => resolveSourceCandidateRequest(r, "dismiss")}
                    style={{
                      padding: "4px 10px",
                      background: "transparent",
                      color: "#374151",
                      border: "1px solid #d4d4d8",
                      borderRadius: 4,
                      fontSize: 12,
                      cursor: "pointer",
                    }}
                    title="Dismiss — engineer should ask differently"
                  >
                    Dismiss
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Phase 5: incoming consumer-pushback notifications. Surface above
          everything else so source POs see consumer feedback prominently
          — these are signals that a deployed v(n) is causing real pain
          for a downstream consumer. */}
      {pushbacks.length > 0 && (
        <div style={{ marginBottom: 16 }}>
          <div style={{ fontSize: 12, color: "#9a3412", textTransform: "uppercase", letterSpacing: 0.4, fontWeight: 600, marginBottom: 8 }}>
            Consumer pushbacks ({pushbacks.length})
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {pushbacks.map((p) => (
              <div
                key={p.request_id}
                style={{
                  padding: 12,
                  background: "#fff7ed",
                  border: "1px solid #fdba74",
                  borderRadius: 8,
                  display: "flex",
                  gap: 12,
                  alignItems: "flex-start",
                }}
              >
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap", marginBottom: 4 }}>
                    <strong style={{ fontSize: 13, color: "#9a3412" }}>
                      Pushback on {p.source_project_name}
                    </strong>
                    <span style={{ fontSize: 11, color: "#92400e" }}>
                      from {p.submitted_by}
                    </span>
                  </div>
                  {p.notes && (
                    <div style={{ fontSize: 13, color: "#374151", whiteSpace: "pre-wrap" }}>
                      {p.notes}
                    </div>
                  )}
                </div>
                <div style={{ display: "flex", gap: 6 }}>
                  <button
                    onClick={() => resolvePushback(p.request_id, "accept")}
                    style={{
                      padding: "4px 10px",
                      background: "#16a34a",
                      color: "white",
                      border: "none",
                      borderRadius: 4,
                      fontSize: 12,
                      fontWeight: 600,
                      cursor: "pointer",
                    }}
                    title="Acknowledge the pushback — you'll revise the contract"
                  >
                    Acknowledge
                  </button>
                  <button
                    onClick={() => resolvePushback(p.request_id, "dismiss")}
                    style={{
                      padding: "4px 10px",
                      background: "transparent",
                      color: "#374151",
                      border: "1px solid #d4d4d8",
                      borderRadius: 4,
                      fontSize: 12,
                      cursor: "pointer",
                    }}
                    title="Dismiss — keep the contract as-is"
                  >
                    Dismiss
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {ingestDrafts.length > 0 && (
        <div style={{ marginBottom: 16 }}>
          <div style={{ fontSize: 12, color: "#64748b", textTransform: "uppercase", letterSpacing: 0.4, fontWeight: 600, marginBottom: 8 }}>
            In-flight ingests
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            {ingestDrafts.map((d) => (
              <IngestDraftCard
                key={d.id}
                draft={d}
                onResume={() => navigate(`/product/ingest?draft=${d.id}`)}
              />
            ))}
          </div>
        </div>
      )}

      {/* Candidate pipeline — feasibility scores flagged for future work */}
      {candidates.length > 0 && (
        <div style={{ marginBottom: 16 }}>
          <div style={{ fontSize: 12, color: "#64748b", textTransform: "uppercase", letterSpacing: 0.4, fontWeight: 600, marginBottom: 8 }}>
            Candidate pipeline ({candidates.length})
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {candidates.map((c) => (
              <FeasibilityCandidateCard
                key={c.id}
                candidate={c}
                onDismiss={() => dismissCandidate(c)}
              />
            ))}
          </div>
        </div>
      )}

      {inFlight.length > 0 && (
        <div style={{ marginBottom: 16 }}>
          <div style={{ fontSize: 12, color: "#64748b", textTransform: "uppercase", letterSpacing: 0.4, fontWeight: 600, marginBottom: 8 }}>
            In flight
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            {inFlight.map((r) => (
              <InFlightCard
                key={r.request_id}
                request={r}
                onDelete={handleDelete}
                isDeleting={deleting === r.project_id}
              />
            ))}
          </div>
        </div>
      )}

      {derivedDrafts.length > 0 && (
        <div style={{ marginBottom: 16 }}>
          <div style={{ fontSize: 12, color: "#64748b", textTransform: "uppercase", letterSpacing: 0.4, fontWeight: 600, marginBottom: 8 }}>
            Derived products — draft
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            {derivedDrafts.map((d) => (
              <DerivedDraftCard
                key={d.project_id}
                draft={d}
                onDelete={handleDelete}
                isDeleting={deleting === d.project_id}
              />
            ))}
          </div>
        </div>
      )}

      {products.length > 0 && inFlight.length > 0 && (
        <div style={{ fontSize: 12, color: "#64748b", textTransform: "uppercase", letterSpacing: 0.4, fontWeight: 600, marginBottom: 8 }}>
          Published / in review
        </div>
      )}

      {analysisFor && (
        <div
          onClick={() => setAnalysisFor(null)}
          style={{
            position: "fixed",
            inset: 0,
            backgroundColor: "rgba(15,23,42,0.55)",
            display: "flex",
            alignItems: "flex-start",
            justifyContent: "center",
            paddingTop: 60,
            zIndex: 1000,
          }}
        >
          <div
            onClick={(e) => e.stopPropagation()}
            style={{
              width: "min(820px, 92vw)",
              maxHeight: "85vh",
              overflow: "auto",
              backgroundColor: "#f8fafc",
              borderRadius: 12,
              padding: 20,
              boxShadow: "0 20px 60px rgba(15,23,42,0.35)",
            }}
          >
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
              <div>
                <div style={{ fontSize: 12, color: "#64748b", textTransform: "uppercase", letterSpacing: 0.4 }}>
                  OSI readiness analysis
                </div>
                <div style={{ fontSize: 18, fontWeight: 700, color: "#0f172a" }}>{analysisFor.productName}</div>
              </div>
              <button
                type="button"
                onClick={() => setAnalysisFor(null)}
                style={{
                  background: "none",
                  border: "none",
                  fontSize: 22,
                  color: "#64748b",
                  cursor: "pointer",
                  lineHeight: 1,
                }}
                aria-label="Close"
              >
                ×
              </button>
            </div>
            <OsiAnalysisPanel projectId={analysisFor.projectId} />
          </div>
        </div>
      )}

      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        {products.map((p) => {
          const canEdit = EDITABLE_STATES.has(p.lifecycle_state) && p.project_id !== null;
          const canDeploy = p.lifecycle_state === "approved" && p.project_id !== null;
          const isDeploying = deploying === p.project_id;
          const detailLink = `/product/marketplace/${encodeURIComponent(p.uri)}`;
          return (
            <div
              key={p.uri}
              style={{
                padding: 16,
                borderRadius: 10,
                border: "1px solid #e2e8f0",
                backgroundColor: "#fff",
                display: "grid",
                gridTemplateColumns: "2fr 1fr 1fr auto",
                gap: 12,
                alignItems: "center",
              }}
            >
              <Link to={detailLink} style={{ textDecoration: "none", color: "inherit" }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <div style={{ fontWeight: 700, color: "#0f172a", fontSize: 15 }}>{p.name}</div>
                  {p.product_kind && <ProductKindChip kind={p.product_kind} />}
                </div>
                <div style={{ fontSize: 12, color: "#64748b", marginTop: 4 }}>
                  {p.description?.slice(0, 140) || "—"}
                </div>
              </Link>
              <Link to={detailLink} style={{ textDecoration: "none", fontSize: 12, color: "#475569" }}>
                <div>
                  <strong>{p.column_count}</strong> columns
                </div>
                {p.published_at && <div style={{ color: "#059669", marginTop: 2 }}>Deployed</div>}
                {p.osi_band && (
                  <div style={{ marginTop: 6 }}>
                    <OsiBadge
                      band={p.osi_band}
                      completeness={p.osi_completeness}
                      conformancePass={p.osi_conformance_pass}
                      size="sm"
                      rubricShortLabel={p.rubric_short_label}
                    />
                  </div>
                )}
              </Link>
              <LifecyclePill state={p.lifecycle_state} />
              <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                {p.project_id !== null && (
                  <button
                    type="button"
                    onClick={() =>
                      setAnalysisFor({ projectId: p.project_id as number, productName: p.name })
                    }
                    style={{
                      padding: "6px 12px",
                      borderRadius: 6,
                      border: "1px solid #cbd5e1",
                      backgroundColor: "#fff",
                      color: "#334155",
                      fontSize: 12,
                      fontWeight: 600,
                      cursor: "pointer",
                    }}
                    title="View OSI readiness analysis"
                  >
                    View Analysis
                  </button>
                )}
                {canEdit && (
                  <Link
                    // SA edit routing:
                    //  - Deployed source products (published / superseded) get
                    //    the Phase 2 EditSourceProductPanel, which lets the PO
                    //    tweak metadata / sensitivity / descriptions without an
                    //    engineer round-trip. Changes flow through the
                    //    classifier (cosmetic → patch, schema/breaking → branch).
                    //  - Source products in initial validation still go to
                    //    PoValidationPage — that surface IS the validation gate.
                    //  - Consumer-aligned (and legacy) edits go to
                    //    NewProductWizard in edit mode.
                    to={p.product_kind === "source"
                      ? (p.lifecycle_state === "published" || p.lifecycle_state === "superseded"
                          ? `/product/edit-source/${p.project_id}`
                          : `/product/validate/${p.project_id}`)
                      : `/product/edit/${p.project_id}`}
                    style={{
                      padding: "6px 12px",
                      borderRadius: 6,
                      border: `1px solid ${productTheme.accent}`,
                      color: productTheme.accent,
                      backgroundColor: "#fff",
                      fontSize: 12,
                      fontWeight: 600,
                      textDecoration: "none",
                    }}
                    title={
                      p.product_kind !== "source"
                        ? (p.lifecycle_state === "approved" || p.lifecycle_state === "published"
                            ? "Open — read-only view; enable editing inside to branch a new version"
                            : "Open")
                        : (p.lifecycle_state === "published" || p.lifecycle_state === "superseded")
                        ? "Open — read-only view; enable editing to change metadata / sensitivity / descriptions"
                        : "Edit — source product validation gate"
                    }
                  >
                    {p.product_kind !== "source"
                      || p.lifecycle_state === "published"
                      || p.lifecycle_state === "superseded"
                      ? "Open"
                      : "Edit"}
                  </Link>
                )}
                {canDeploy && p.project_id !== null && (
                  <button
                    type="button"
                    onClick={() =>
                      // Derived products (consumer + aggregate) compose upstreams
                      // → pre-deploy checklist; source products deploy directly.
                      p.product_kind && p.product_kind !== "source"
                        ? setDeployChecklistFor(p.project_id as number)
                        : deploy(p.project_id as number)
                    }
                    disabled={isDeploying}
                    style={{
                      padding: "6px 12px",
                      borderRadius: 6,
                      border: "none",
                      backgroundColor: "#16a34a",
                      color: "#fff",
                      fontSize: 12,
                      fontWeight: 600,
                      cursor: isDeploying ? "wait" : "pointer",
                      opacity: isDeploying ? 0.6 : 1,
                    }}
                    title="Deploy to the marketplace"
                  >
                    {isDeploying ? "Deploying..." : "Deploy"}
                  </button>
                )}
                {p.discardable_draft && p.project_id !== null && (
                  <button
                    type="button"
                    onClick={() => handleDiscardDraft(p.project_id as number, p.name)}
                    disabled={discarding === p.project_id}
                    style={{
                      padding: "6px 12px",
                      borderRadius: 6,
                      border: "1px solid #fed7aa",
                      backgroundColor: "#fff",
                      color: "#c2410c",
                      fontSize: 12,
                      fontWeight: 600,
                      cursor: discarding === p.project_id ? "wait" : "pointer",
                      opacity: discarding === p.project_id ? 0.6 : 1,
                    }}
                    title="Discard the in-flight draft version and roll back to the previous version"
                  >
                    {discarding === p.project_id ? "Discarding…" : "Discard draft"}
                  </button>
                )}
                {p.project_id !== null && (
                  <button
                    type="button"
                    onClick={() => handleDelete(p.project_id as number, p.name)}
                    disabled={deleting === p.project_id}
                    style={{
                      padding: "6px 12px",
                      borderRadius: 6,
                      border: "1px solid #fecaca",
                      backgroundColor: "#fff",
                      color: "#b91c1c",
                      fontSize: 12,
                      fontWeight: 600,
                      cursor: deleting === p.project_id ? "wait" : "pointer",
                      opacity: deleting === p.project_id ? 0.6 : 1,
                    }}
                    title="Permanently delete this product (graph, project record, and folder)"
                  >
                    {deleting === p.project_id ? "Deleting..." : "Delete"}
                  </button>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function InFlightCard({
  request,
  onDelete,
  isDeleting,
}: {
  request: InFlightRequest;
  onDelete: (projectId: number, productName: string) => void;
  isDeleting: boolean;
}) {
  const ready = request.ready_for_validation;
  const submitted = request.submitted_at ? new Date(request.submitted_at).toLocaleDateString() : null;
  const accent = ready ? "#7c3aed" : "#475569";
  const bgTint = ready ? "#f5f3ff" : "#f8fafc";
  return (
    <div
      style={{
        padding: 14,
        borderRadius: 10,
        border: `1px solid ${ready ? "#c4b5fd" : "#e2e8f0"}`,
        backgroundColor: bgTint,
        display: "grid",
        gridTemplateColumns: "1fr auto",
        gap: 12,
        alignItems: "center",
      }}
    >
      <div>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <div style={{ fontWeight: 700, color: "#0f172a", fontSize: 15 }}>{request.name}</div>
          <ProductKindChip kind="source" />
        </div>
        <div style={{ fontSize: 12, color: "#475569", marginTop: 4, display: "flex", gap: 12, flexWrap: "wrap" }}>
          {request.domain && <span>domain: <strong style={{ color: "#334155" }}>{request.domain}</strong></span>}
          {submitted && <span>submitted {submitted}</span>}
          <span style={{ color: accent, fontWeight: 600 }}>{request.status_label}</span>
        </div>
        {request.product_idea && (
          <div
            style={{
              fontSize: 12,
              color: "#64748b",
              marginTop: 6,
              maxWidth: 600,
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
            title={request.product_idea}
          >
            {request.product_idea}
          </div>
        )}
      </div>
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        {ready ? (
          <Link
            to={`/product/validate/${request.project_id}`}
            style={{
              padding: "8px 14px",
              borderRadius: 6,
              backgroundColor: productTheme.accent,
              color: "#fff",
              textDecoration: "none",
              fontSize: 13,
              fontWeight: 600,
              whiteSpace: "nowrap",
            }}
          >
            Validate now →
          </Link>
        ) : (
          <span
            style={{
              padding: "6px 10px",
              borderRadius: 6,
              backgroundColor: "#fff",
              border: "1px solid #e2e8f0",
              color: "#94a3b8",
              fontSize: 12,
              fontWeight: 500,
              whiteSpace: "nowrap",
            }}
          >
            Engineer working
          </span>
        )}
        <button
          type="button"
          onClick={() => onDelete(request.project_id, request.name)}
          disabled={isDeleting}
          style={{
            padding: "6px 12px",
            borderRadius: 6,
            border: "1px solid #fecaca",
            backgroundColor: "#fff",
            color: "#b91c1c",
            fontSize: 12,
            fontWeight: 600,
            cursor: isDeleting ? "wait" : "pointer",
            opacity: isDeleting ? 0.6 : 1,
            whiteSpace: "nowrap",
          }}
          title="Permanently delete this product (graph, project record, and folder)"
        >
          {isDeleting ? "Deleting..." : "Delete"}
        </button>
      </div>
    </div>
  );
}

function DerivedDraftCard({
  draft,
  onDelete,
  isDeleting,
}: {
  draft: DerivedDraft;
  onDelete: (projectId: number, productName: string) => void;
  isDeleting: boolean;
}) {
  const total = draft.dependencies_total;
  const ready = draft.dependencies_ready;
  const bound = draft.dependencies_bound ?? 0;
  const allReady = total > 0 && ready === total;
  const readinessColor = total === 0 ? "#64748b" : allReady ? "#16a34a" : "#b45309";
  const readinessText =
    (total === 0
      ? "no upstream sources"
      : allReady
      ? `${ready} of ${total} source product(s) published — ready to wire`
      : `${ready} of ${total} source product(s) published — waiting on ${total - ready}`) +
    // 'bound' = the :CONSUMES edge is already wired (flipped on a consumer save).
    (bound > 0 ? ` · ${bound} wired` : "");
  return (
    <div
      style={{
        padding: 14,
        borderRadius: 10,
        border: "1px solid #ddd6fe",
        backgroundColor: "#f5f3ff",
        display: "grid",
        gridTemplateColumns: "1fr auto",
        gap: 12,
        alignItems: "center",
      }}
    >
      <div>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <div style={{ fontWeight: 700, color: "#0f172a", fontSize: 15 }}>{draft.name}</div>
          <span style={{ fontSize: 11, fontWeight: 700, color: "#6d28d9", background: "#ede9fe", border: "1px solid #ddd6fe", borderRadius: 999, padding: "1px 8px" }}>
            Derived
          </span>
          {draft.from_intake && (
            <span style={{ fontSize: 11, color: "#4338ca", background: "#eef2ff", border: "1px solid #c7d2fe", borderRadius: 999, padding: "1px 8px" }}>
              From intake
            </span>
          )}
        </div>
        <div style={{ fontSize: 12, color: "#475569", marginTop: 4, display: "flex", gap: 12, flexWrap: "wrap" }}>
          {draft.domain && <span>domain: <strong style={{ color: "#334155" }}>{draft.domain}</strong></span>}
          <span style={{ color: readinessColor, fontWeight: 600 }}>{readinessText}</span>
        </div>
        {draft.product_idea && (
          <div
            style={{ fontSize: 12, color: "#64748b", marginTop: 6, maxWidth: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
            title={draft.product_idea}
          >
            {draft.product_idea}
          </div>
        )}
      </div>
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <Link
          to={`/product/edit/${draft.project_id}`}
          style={{
            padding: "8px 14px",
            borderRadius: 6,
            backgroundColor: productTheme.accent,
            color: "#fff",
            textDecoration: "none",
            fontSize: 13,
            fontWeight: 600,
            whiteSpace: "nowrap",
          }}
        >
          Open in wizard →
        </Link>
        <button
          type="button"
          onClick={() => onDelete(draft.project_id, draft.name)}
          disabled={isDeleting}
          style={{
            padding: "6px 12px",
            borderRadius: 6,
            border: "1px solid #fecaca",
            backgroundColor: "#fff",
            color: "#b91c1c",
            fontSize: 12,
            fontWeight: 600,
            cursor: isDeleting ? "wait" : "pointer",
            opacity: isDeleting ? 0.6 : 1,
            whiteSpace: "nowrap",
          }}
          title="Permanently delete this draft product (graph, project record, and folder)"
        >
          {isDeleting ? "Deleting..." : "Delete"}
        </button>
      </div>
    </div>
  );
}

const ctaPrimary: React.CSSProperties = {
  padding: "8px 14px", borderRadius: 6, backgroundColor: productTheme.accent,
  color: "#fff", textDecoration: "none", fontSize: 13, fontWeight: 600,
};
const ctaSecondary: React.CSSProperties = {
  padding: "8px 14px", borderRadius: 6, backgroundColor: "#fff",
  color: productTheme.accent, textDecoration: "none", fontSize: 13, fontWeight: 600,
  border: `1px solid ${productTheme.accent}`,
};
const ctaTertiary: React.CSSProperties = {
  padding: "8px 14px", borderRadius: 6, backgroundColor: "#fff",
  color: "#475569", textDecoration: "none", fontSize: 13, fontWeight: 500,
  border: "1px solid #cbd5e1",
};

function FeasibilityCandidateCard({
  candidate,
  onDismiss,
}: {
  candidate: FeasibilityCandidate;
  onDismiss: () => void;
}) {
  const t = CANDIDATE_TIER[candidate.tier] || CANDIDATE_TIER.absent;
  const reqCovPct = Math.round(candidate.required_coverage * 100);
  const confidencePct = Math.round(candidate.confidence * 100);
  const savedDate = candidate.created_at ? new Date(candidate.created_at).toLocaleDateString() : null;
  return (
    <div
      style={{
        padding: 14,
        borderRadius: 10,
        border: "1px solid #e2e8f0",
        backgroundColor: "#fff",
        display: "flex",
        flexDirection: "column",
        gap: 6,
      }}
    >
      <div style={{ display: "flex", alignItems: "flex-start", gap: 10 }}>
        <span
          style={{
            fontSize: 11, fontWeight: 700, padding: "2px 9px", borderRadius: 5,
            background: t.bg, color: t.fg, whiteSpace: "nowrap", flexShrink: 0,
          }}
        >
          {t.label}
        </span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontWeight: 700, color: "#0f172a", fontSize: 14 }}>
            {candidate.spec_name}
          </div>
          {candidate.domain && (
            <div style={{ fontSize: 11, color: "#94a3b8" }}>{candidate.domain}</div>
          )}
        </div>
        <div style={{ fontSize: 11, color: "#64748b", textAlign: "right", flexShrink: 0 }}>
          <div>Coverage {reqCovPct}%</div>
          <div>Confidence {confidencePct}%</div>
        </div>
      </div>
      {/* Coverage bar */}
      <div style={{ height: 6, borderRadius: 3, background: "#e2e8f0", overflow: "hidden" }}>
        <div style={{ height: 6, width: `${reqCovPct}%`, background: t.bg }} />
      </div>
      {candidate.rationale && (
        <div
          style={{ fontSize: 12, color: "#475569", overflow: "hidden", display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical" }}
          title={candidate.rationale}
        >
          {candidate.rationale}
        </div>
      )}
      {candidate.notes && (
        <div style={{ fontSize: 12, color: "#7c3aed", fontStyle: "italic" }}>"{candidate.notes}"</div>
      )}
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 2 }}>
        {savedDate && <span style={{ fontSize: 11, color: "#94a3b8" }}>Saved {savedDate}</span>}
        <span style={{ flex: 1 }} />
        <Link
          to={`/product/feasibility?estate_id=${candidate.estate_id}`}
          style={{
            fontSize: 12, fontWeight: 600, padding: "4px 10px", borderRadius: 5,
            border: "1px solid #cbd5e1", color: "#475569", textDecoration: "none",
          }}
          title="Go to feasibility page for this estate"
        >
          → Evaluate again
        </Link>
        <button
          type="button"
          onClick={onDismiss}
          style={{
            fontSize: 12, fontWeight: 600, padding: "4px 10px", borderRadius: 5,
            border: "1px solid #fecaca", color: "#b91c1c", background: "#fff", cursor: "pointer",
          }}
          title="Dismiss this candidate"
        >
          Dismiss
        </button>
      </div>
    </div>
  );
}

function LifecyclePill({ state }: { state: string }) {
  const key = state || "draft";
  const palette = LIFECYCLE_COLORS[key] || LIFECYCLE_COLORS.draft;
  const label = LIFECYCLE_LABEL[key] || key;
  return (
    <span
      style={{
        padding: "4px 10px",
        borderRadius: 999,
        backgroundColor: palette.bg,
        color: palette.fg,
        fontSize: 12,
        fontWeight: 600,
        justifySelf: "end",
      }}
    >
      {label}
    </span>
  );
}

/**
 * Card for a persisted in-flight IngestDraft. Renders the spec name, the
 * classification chip (source/consumer), and progress towards binding the
 * required source-product slots. Clicking the card resumes the ingest via
 * /product/ingest?draft=<id>.
 */
function IngestDraftCard({
  draft,
  onResume,
}: {
  draft: IngestDraftRow;
  onResume: () => void;
}) {
  const { specName, classification, slots, lastUpdated } = useMemo(() => {
    let specName = "(unnamed)";
    try {
      const spec = JSON.parse(draft.parsed_spec_json || "{}");
      if (typeof spec?.name === "string" && spec.name.trim()) {
        specName = spec.name;
      }
    } catch {
      // ignore
    }
    let classification: IngestClassification | null = null;
    try {
      classification = draft.classification_json
        ? (JSON.parse(draft.classification_json) as IngestClassification)
        : null;
    } catch {
      classification = null;
    }
    let slots: ResolveSlot[] = [];
    try {
      const arr = JSON.parse(draft.input_selections_json || "[]");
      if (Array.isArray(arr)) slots = arr as ResolveSlot[];
    } catch {
      slots = [];
    }
    const lastUpdated = draft.updated_at ? new Date(draft.updated_at).toLocaleString() : null;
    return { specName, classification, slots, lastUpdated };
  }, [draft]);

  const matchedCount = slots.filter(
    (s) => s.resolution === "matched" && s.selected_uri,
  ).length;
  const totalCount = slots.length;
  const isConsumer = (draft.archetype_choice || "").toLowerCase() === "dpe-cf";

  return (
    <button
      type="button"
      onClick={onResume}
      style={{
        textAlign: "left",
        background: "#fff",
        border: "1px solid #c7d2fe",
        borderLeft: "4px solid #6366f1",
        borderRadius: 10,
        padding: 14,
        cursor: "pointer",
        display: "flex",
        flexDirection: "column",
        gap: 6,
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 10 }}>
        <div style={{ minWidth: 0 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            <span style={{ fontSize: 14, fontWeight: 700, color: "#0f172a" }}>{specName}</span>
            {isConsumer ? (
              <span
                style={{
                  padding: "2px 8px",
                  borderRadius: 999,
                  fontSize: 10,
                  fontWeight: 700,
                  backgroundColor: "#fef3c7",
                  color: "#854d0e",
                  border: "1px solid #fde68a",
                }}
                title="Derived-product ingest (aggregate or consumer — chosen in the wizard)"
              >
                Derived
              </span>
            ) : (
              <span
                style={{
                  padding: "2px 8px",
                  borderRadius: 999,
                  fontSize: 10,
                  fontWeight: 700,
                  backgroundColor: "#dbeafe",
                  color: "#1d4ed8",
                  border: "1px solid #93c5fd",
                }}
                title="Source-aligned ingest"
              >
                Source
              </span>
            )}
            {classification && classification.confidence > 0 && (
              <span style={{ fontSize: 11, color: "#64748b" }}>
                {classification.confidence}% confidence
              </span>
            )}
          </div>
          {isConsumer && (
            <div style={{ fontSize: 12, color: "#475569", marginTop: 4 }}>
              {totalCount === 0
                ? "No source dependencies detected yet — classify to discover."
                : matchedCount === totalCount
                ? `All ${totalCount} source product${totalCount === 1 ? "" : "s"} bound — ready to submit.`
                : `${matchedCount} of ${totalCount} source product${totalCount === 1 ? "" : "s"} bound.`}
            </div>
          )}
          {lastUpdated && (
            <div style={{ fontSize: 11, color: "#94a3b8", marginTop: 4 }}>
              Last updated {lastUpdated}
            </div>
          )}
        </div>
        <span
          style={{
            padding: "4px 10px",
            borderRadius: 999,
            backgroundColor: matchedCount === totalCount && totalCount > 0 ? "#dcfce7" : "#eef2ff",
            color: matchedCount === totalCount && totalCount > 0 ? "#065f46" : "#4338ca",
            fontSize: 11,
            fontWeight: 700,
            whiteSpace: "nowrap",
          }}
        >
          {matchedCount === totalCount && totalCount > 0 ? "Ready" : "Resume →"}
        </span>
      </div>
    </button>
  );
}
