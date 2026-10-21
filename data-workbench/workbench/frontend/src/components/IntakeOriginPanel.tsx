/**
 * Shows a project's inbound-intake provenance: the origin (source system +
 * external ref), the (editable-at-source) narrative summary, a "View full
 * intake" link back to the read-only review page, and the *candidate-scoped*
 * parsed dataset/column inventory the external assessment provided. Self-hides
 * (404) for projects that weren't scaffolded from intake.
 *
 * Important: this inventory is the PRE-DISCOVERY expectation, not graph truth.
 * The scaffold deliberately writes no :Dataset/:Column nodes (discovery, via
 * MERGE ON CREATE, is authoritative) — so this panel keeps the input visible
 * and lets the engineer compare it against what Data Discovery actually finds.
 *
 * The inventory is candidate-scoped by the backend: a modernization child
 * (dpe-sa / dpe-cf) shows only its own candidate's datasets; migration shows
 * the top-level blueprint datasets.
 */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import api from "../api/client";
import type { DatasetCandidate } from "../pages/intake/types";

interface IntakeOrigin {
  intake_id: number;
  source_system: string;
  external_ref: string;
  scenario: string;
  rationale: string;
  spawn_kind: string;
  candidate_id: string;
  dataset_count: number;
  column_count: number;
  datasets: DatasetCandidate[];
  submitted_at: string | null;
}

export default function IntakeOriginPanel({ projectId }: { projectId: number }) {
  const [origin, setOrigin] = useState<IntakeOrigin | null>(null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api
      .get(`/api/intake/origin/by-project/${projectId}`)
      .then((res) => {
        if (!cancelled) setOrigin(res.data as IntakeOrigin);
      })
      .catch(() => {
        if (!cancelled) setOrigin(null); // 404 = not intake-originated
      });
    return () => {
      cancelled = true;
    };
  }, [projectId]);

  if (!origin) return null;

  const datasets = origin.datasets || [];
  // Migration → engineer intake surface; modernization → PO intake surface.
  const reviewHref =
    origin.scenario === "modernization"
      ? `/product/intake/${origin.intake_id}`
      : `/engineer/intake/${origin.intake_id}`;
  // A Connected-Estate assembly scaffold pre-binds the source connection and
  // pre-scopes Data Discovery to the cluster's tables (Bridge B) — the banner copy
  // reflects that rather than "you still have to pick the source".
  const estateSaScaffold =
    origin.source_system === "connected-estate" && origin.spawn_kind === "dpe-sa";

  return (
    <div style={{ border: "1px solid #c7d2fe", background: "#eef2ff", borderRadius: 10, padding: "10px 14px", marginBottom: 12 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <span style={{ fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: 0.4, color: "#4338ca" }}>
          Scaffolded from intake
        </span>
        <span style={{ fontSize: 12, color: "#475569" }}>
          {origin.source_system} · {origin.external_ref}
        </span>
        <div style={{ flex: 1 }} />
        <span style={{ fontSize: 12, color: "#64748b" }}>
          {origin.dataset_count} table(s) · {origin.column_count} column(s) reported
        </span>
        <Link
          to={reviewHref}
          style={{ fontSize: 12, padding: "3px 10px", borderRadius: 6, border: "1px solid #c7d2fe", background: "#fff", color: "#4338ca", textDecoration: "none" }}
        >
          View full intake →
        </Link>
        {datasets.length > 0 && (
          <button
            onClick={() => setOpen((o) => !o)}
            style={{ fontSize: 12, padding: "3px 10px", borderRadius: 6, border: "1px solid #c7d2fe", background: "#fff", color: "#4338ca", cursor: "pointer" }}
          >
            {open ? "Hide inventory" : "Show inventory"}
          </button>
        )}
      </div>

      {origin.rationale ? (
        <div style={{ fontSize: 13, color: "#334155", marginTop: 8, fontStyle: "italic" }}>{origin.rationale}</div>
      ) : null}

      <div style={{ fontSize: 12, color: "#64748b", marginTop: 6 }}>
        {estateSaScaffold ? (
          <>
            The source connection is <strong>already bound</strong> and Data Discovery is{" "}
            <strong>pre-scoped</strong> to this cluster's tables. Just run <strong>Data Discovery</strong>{" "}
            for the deep pass (primary keys, foreign keys, then profiling-informed descriptions) — the
            estate scan only captured names and types. The Project Summary below reflects discovered
            data, so it stays at zero until discovery runs.
          </>
        ) : (
          <>
            This is the <strong>expected</strong> inventory from the external assessment — a reference,
            not graph truth. Run <strong>Data Discovery</strong> to profile the real source; the Project
            Summary below reflects discovered data, so it stays at zero until then.
          </>
        )}
      </div>

      {open && datasets.length > 0 && (
        <div style={{ marginTop: 10, display: "flex", flexDirection: "column", gap: 8 }}>
          {datasets.map((ds, i) => (
            <div key={ds.candidate_id || i} style={{ background: "#fff", border: "1px solid #e2e8f0", borderRadius: 8, padding: 10 }}>
              <div style={{ fontFamily: "monospace", fontWeight: 600, fontSize: 13, marginBottom: 6 }}>
                {ds.name?.value || "(unnamed)"}
                {ds.incremental_cursor?.value ? (
                  <span style={{ fontWeight: 400, color: "#64748b", marginLeft: 8 }}>cursor: {ds.incremental_cursor.value}</span>
                ) : null}
              </div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                {(ds.columns || []).map((c, j) => (
                  <span key={c.candidate_id || j} style={{ fontSize: 11, background: "#f1f5f9", border: "1px solid #e2e8f0", borderRadius: 4, padding: "2px 6px", fontFamily: "monospace" }}>
                    {c.name?.value}
                    {c.data_type?.value ? <span style={{ color: "#94a3b8" }}> : {c.data_type.value}</span> : null}
                    {c.note ? <span style={{ color: "#94a3b8" }}> · {c.note}</span> : null}
                  </span>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
