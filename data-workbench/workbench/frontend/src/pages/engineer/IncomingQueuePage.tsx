import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import api from "../../api/client";
import { useCurrentUserEmail } from "../../AuthContext";
import { engineerTheme } from "../../theme";
import RejectDialog from "./RejectDialog";

type Kind = "new" | "edit" | "ingest";
type Status = "submitted" | "accepted" | "complete" | "rejected";

interface ProductRequest {
  id: number;
  project_id: number;
  project_code: string | null;
  project_name: string | null;
  archetype: string | null;
  contract_id: string;
  versioned_id: string | null;
  kind: Kind;
  status: Status;
  submitted_by: string;
  submitted_at: string | null;
  accepted_by: string | null;
  accepted_at: string | null;
  completed_at: string | null;
  engineer_assigned: string | null;
  notes: string | null;
}

const KIND_LABEL: Record<Kind, string> = {
  new: "New product",
  edit: "Edit existing",
  ingest: "Ingest existing",
};

const KIND_COLORS: Record<Kind, { bg: string; fg: string }> = {
  new: { bg: "#dbeafe", fg: "#1d4ed8" },
  edit: { bg: "#fef3c7", fg: "#92400e" },
  ingest: { bg: "#ede9fe", fg: "#5b21b6" },
};

/**
 * Engineering incoming queue. Shows submitted product requests first
 * (actionable) and a collapsed history of in-flight / completed work
 * below. Accepting a submitted request flips the contract's
 * lifecycleState to ``in_engineering`` and drops the engineer into
 * the project detail page to run the technical pipeline.
 */
export default function IncomingQueuePage() {
  const navigate = useNavigate();
  const CURRENT_ENGINEER = useCurrentUserEmail();
  const [requests, setRequests] = useState<ProductRequest[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [rejectTarget, setRejectTarget] = useState<ProductRequest | null>(null);
  const [rejectSubmitting, setRejectSubmitting] = useState(false);

  const reload = useCallback(async () => {
    try {
      const res = await api.get("/api/product-requests");
      setRequests(res.data.requests || []);
      setError(null);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  const accept = async (r: ProductRequest) => {
    setBusyId(r.id);
    try {
      await api.post(`/api/product-requests/${r.id}/accept`, { engineer: CURRENT_ENGINEER });
      // Drop the engineer into the project view to start the pipeline.
      navigate(`/engineer/projects/${r.project_id}`);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyId(null);
    }
  };

  const openReject = (r: ProductRequest) => {
    setRejectTarget(r);
  };

  const submitReject = async (payload: { category: string; reason: string }) => {
    if (!rejectTarget) return;
    const target = rejectTarget;
    setRejectSubmitting(true);
    setBusyId(target.id);
    try {
      await api.post(`/api/product-requests/${target.id}/reject`, {
        category: payload.category,
        reason: payload.reason,
        engineer: CURRENT_ENGINEER,
      });
      setRejectTarget(null);
      await reload();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRejectSubmitting(false);
      setBusyId(null);
    }
  };

  const markComplete = async (r: ProductRequest) => {
    setBusyId(r.id);
    try {
      await api.post(`/api/product-requests/${r.id}/complete`, {});
      await reload();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyId(null);
    }
  };

  const submitted = requests.filter((r) => r.status === "submitted");
  const accepted = requests.filter((r) => r.status === "accepted");
  const recent = requests.filter((r) => r.status === "complete" || r.status === "rejected").slice(0, 10);

  return (
    <div style={{ maxWidth: 1080, margin: "0 auto" }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 16 }}>
        <div>
          <h1 style={{ fontSize: 22, fontWeight: 700, color: "#0f172a", margin: 0 }}>Incoming product requests</h1>
          <div style={{ fontSize: 13, color: "#64748b", marginTop: 4 }}>
            Product owners send requests here. Accepting one flips the contract into
            engineering and takes you to the project.
          </div>
        </div>
        <button
          type="button"
          onClick={reload}
          style={{
            padding: "6px 14px",
            border: "1px solid #cbd5e1",
            borderRadius: 6,
            backgroundColor: "#fff",
            fontSize: 13,
            fontWeight: 600,
            color: "#334155",
            cursor: "pointer",
          }}
        >
          Refresh
        </button>
      </div>

      {error && (
        <div style={{ color: "#dc2626", backgroundColor: "#fef2f2", padding: 12, borderRadius: 6, marginBottom: 12 }}>
          {error}
        </div>
      )}
      {loading && <div style={{ color: "#64748b" }}>Loading…</div>}

      <Section title={`Awaiting acceptance (${submitted.length})`} subtitle="You haven't started these yet.">
        {submitted.length === 0 && <Empty text="No incoming requests. Product owners' submissions will appear here." />}
        {submitted.map((r) => (
          <Row key={r.id} r={r} busy={busyId === r.id}>
            <button
              type="button"
              onClick={() => accept(r)}
              disabled={busyId === r.id}
              style={primaryBtn(engineerTheme.accent)}
            >
              Accept & open project
            </button>
            <button
              type="button"
              onClick={() => openReject(r)}
              disabled={busyId === r.id}
              style={secondaryBtn}
            >
              Reject…
            </button>
          </Row>
        ))}
      </Section>

      <Section title={`In engineering (${accepted.length})`} subtitle="You've accepted these; finish them off to close the loop.">
        {accepted.length === 0 && <Empty text="Nothing in progress." />}
        {accepted.map((r) => (
          <Row key={r.id} r={r} busy={busyId === r.id}>
            <button
              type="button"
              onClick={() => navigate(`/engineer/projects/${r.project_id}`)}
              style={primaryBtn(engineerTheme.accent)}
            >
              Open project
            </button>
            <button
              type="button"
              onClick={() => markComplete(r)}
              disabled={busyId === r.id}
              style={secondaryBtn}
              title="Mark this handoff complete — lifecycleState becomes 'approved'"
            >
              Mark complete
            </button>
          </Row>
        ))}
      </Section>

      {recent.length > 0 && (
        <Section title="Recent history" subtitle="Last 10 completed or rejected requests.">
          {recent.map((r) => (
            <Row key={r.id} r={r} muted />
          ))}
        </Section>
      )}

      <RejectDialog
        open={rejectTarget !== null}
        onClose={() => {
          if (!rejectSubmitting) setRejectTarget(null);
        }}
        subject={
          rejectTarget
            ? `${rejectTarget.project_name || rejectTarget.project_code} · ${rejectTarget.kind}`
            : undefined
        }
        submitting={rejectSubmitting}
        onSubmit={submitReject}
      />
    </div>
  );
}

function Section({ title, subtitle, children }: { title: string; subtitle?: string; children: React.ReactNode }) {
  return (
    <section style={{ marginBottom: 28 }}>
      <div style={{ display: "flex", alignItems: "baseline", gap: 10, marginBottom: 10 }}>
        <h2 style={{ fontSize: 15, fontWeight: 700, color: "#0f172a", margin: 0 }}>{title}</h2>
        {subtitle && <span style={{ fontSize: 12, color: "#94a3b8" }}>{subtitle}</span>}
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>{children}</div>
    </section>
  );
}

function Empty({ text }: { text: string }) {
  return (
    <div style={{ padding: 18, border: "1px dashed #cbd5e1", borderRadius: 8, color: "#64748b", fontSize: 13, textAlign: "center" }}>
      {text}
    </div>
  );
}

function Row({
  r,
  children,
  busy = false,
  muted = false,
}: {
  r: ProductRequest;
  children?: React.ReactNode;
  busy?: boolean;
  muted?: boolean;
}) {
  const kindPalette = KIND_COLORS[r.kind];
  return (
    <div
      style={{
        padding: 14,
        borderRadius: 10,
        border: "1px solid #e2e8f0",
        backgroundColor: muted ? "#f8fafc" : "#fff",
        display: "grid",
        gridTemplateColumns: "1fr auto",
        gap: 14,
        opacity: busy ? 0.6 : 1,
      }}
    >
      <div>
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
          <span style={kindChipStyle(kindPalette.bg, kindPalette.fg)}>{KIND_LABEL[r.kind]}</span>
          {r.archetype && <ArchetypeChip archetype={r.archetype} />}
          <span style={{ fontWeight: 700, color: "#0f172a", fontSize: 14 }}>
            {r.project_name || r.project_code || `Project ${r.project_id}`}
          </span>
          <span style={{ color: "#94a3b8", fontSize: 12 }}>
            {r.project_code}
          </span>
        </div>
        <div style={{ fontSize: 12, color: "#475569" }}>
          Submitted by <strong>{r.submitted_by}</strong>
          {r.submitted_at && <> · {new Date(r.submitted_at).toLocaleString()}</>}
          {r.status === "accepted" && r.engineer_assigned && <> · accepted by {r.engineer_assigned}</>}
          {r.status === "complete" && r.completed_at && <> · completed {new Date(r.completed_at).toLocaleString()}</>}
        </div>
        {r.notes && (
          <div style={{ fontSize: 12, color: "#334155", marginTop: 6, whiteSpace: "pre-wrap" }}>{r.notes}</div>
        )}
      </div>
      {children && (
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>{children}</div>
      )}
    </div>
  );
}

function ArchetypeChip({ archetype }: { archetype: string }) {
  const isSource = archetype === "dpe-sa";
  const isConsumer = archetype === "dpe-cf";
  if (!isSource && !isConsumer) return null;
  return (
    <span
      style={{
        fontSize: 10,
        fontWeight: 700,
        letterSpacing: 0.4,
        textTransform: "uppercase",
        padding: "2px 8px",
        borderRadius: 3,
        backgroundColor: isSource ? "#dbeafe" : "#fde4d4",
        color: isSource ? "#1d4ed8" : "#b86a1d",
      }}
      title={isSource
        ? "Source-aligned product — discovery-first; PO validates names, descriptions, rules."
        : "Consumer-aligned product — composed from already-published source products."}
    >
      {isSource ? "Source-aligned" : "Consumer-aligned"}
    </span>
  );
}

function kindChipStyle(bg: string, fg: string): React.CSSProperties {
  return {
    fontSize: 10,
    fontWeight: 700,
    letterSpacing: 0.4,
    textTransform: "uppercase",
    padding: "2px 8px",
    borderRadius: 3,
    backgroundColor: bg,
    color: fg,
  };
}

function primaryBtn(color: string): React.CSSProperties {
  return {
    padding: "6px 14px",
    borderRadius: 6,
    backgroundColor: color,
    color: "#fff",
    border: "none",
    fontSize: 13,
    fontWeight: 600,
    cursor: "pointer",
  };
}

const secondaryBtn: React.CSSProperties = {
  padding: "6px 14px",
  borderRadius: 6,
  backgroundColor: "#fff",
  color: "#334155",
  border: "1px solid #cbd5e1",
  fontSize: 13,
  fontWeight: 600,
  cursor: "pointer",
};
