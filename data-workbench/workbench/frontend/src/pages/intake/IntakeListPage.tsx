/**
 * Incoming intake queue for a shell. Migration submissions surface in the
 * Engineering shell; modernization submissions in the Product shell (the
 * scenario is fixed per mount). Each row links to the blueprint review page.
 */
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import api from "../../api/client";
import ConfidenceChip from "../../components/ConfidenceChip";
import type { IntakeSubmission } from "./types";

interface Props {
  scenario: "migration" | "modernization";
  basePath: string; // "/engineer" | "/product"
  accent: string;
  title: string;
  blurb: string;
}

const STATUS_STYLE: Record<string, { bg: string; fg: string }> = {
  received: { bg: "#f1f5f9", fg: "#475569" },
  parsing: { bg: "#eef2ff", fg: "#4338ca" },
  proposed: { bg: "#ecfdf5", fg: "#065f46" },
  reviewing: { bg: "#fefce8", fg: "#854d0e" },
  scaffolding: { bg: "#eef2ff", fg: "#4338ca" },
  scaffolded: { bg: "#dcfce7", fg: "#166534" },
  parse_failed: { bg: "#fef2f2", fg: "#991b1b" },
  rejected: { bg: "#f1f5f9", fg: "#64748b" },
};

function StatusChip({ status }: { status: string }) {
  const s = STATUS_STYLE[status] || { bg: "#f1f5f9", fg: "#475569" };
  return (
    <span style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: 0.4, background: s.bg, color: s.fg, borderRadius: 3, padding: "2px 7px" }}>
      {status.replace(/_/g, " ")}
    </span>
  );
}

export default function IntakeListPage({ scenario, basePath, accent, title, blurb }: Props) {
  const [rows, setRows] = useState<IntakeSubmission[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await api.get("/api/intake", { params: { scenario } });
      setRows(res.data?.submissions || []);
      setError(null);
    } catch {
      setError("Failed to load intake queue.");
    } finally {
      setLoading(false);
    }
  }, [scenario]);

  useEffect(() => {
    load();
    const id = window.setInterval(load, 8000); // parse is async — refresh gently
    return () => window.clearInterval(id);
  }, [load]);

  return (
    <div style={{ maxWidth: 900, margin: "0 auto", padding: "24px 20px" }}>
      <h1 style={{ fontSize: 22, margin: "0 0 4px" }}>{title}</h1>
      <p style={{ color: "#64748b", fontSize: 14, marginTop: 0 }}>{blurb}</p>

      {loading ? (
        <div style={{ color: "#64748b", padding: 20 }}>Loading…</div>
      ) : error ? (
        <div style={{ color: "#991b1b", padding: 20 }}>{error}</div>
      ) : rows.length === 0 ? (
        <div style={{ border: "1px dashed #cbd5e1", borderRadius: 10, padding: 28, textAlign: "center", color: "#94a3b8" }}>
          No {scenario} submissions yet. An external tool POSTs to <code>/api/intake/submit</code> to add work here.
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {rows.map((r) => (
            <Link
              key={r.id}
              to={`${basePath}/intake/${r.id}`}
              style={{ textDecoration: "none", color: "inherit" }}
            >
              <div style={{ border: "1px solid #e2e8f0", borderRadius: 10, padding: 14, background: "#fff", display: "grid", gridTemplateColumns: "1fr auto", gap: 8, alignItems: "center" }}>
                <div>
                  <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <StatusChip status={r.status} />
                    {r.blueprint ? <ConfidenceChip confidence={r.blueprint.overall_confidence} compact /> : null}
                    <span style={{ fontWeight: 600, fontSize: 14 }}>{r.source_system}</span>
                    <span style={{ color: "#94a3b8", fontSize: 12 }}>· {r.external_ref}</span>
                  </div>
                  <div style={{ fontSize: 12, color: "#94a3b8", marginTop: 4 }}>
                    updated {r.updated_at ? new Date(r.updated_at).toLocaleString() : "—"}
                  </div>
                </div>
                <span style={{ fontSize: 13, fontWeight: 600, color: accent }}>Review →</span>
              </div>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
