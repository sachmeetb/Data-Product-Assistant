import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import api from "../api/client";
import { useRole } from "../RoleContext";
import type { Archetype } from "../types";

interface TaskItem {
  project_id: number; project_name: string; project_code: string;
  stage_number: number; stage_name: string; stage_id: string;
}

interface ReviewItem {
  project_id: number; project_name: string; project_code: string;
  stage_number: number; stage_name: string; review_type: string;
}

interface DashboardData {
  persona: { role: string; description: string };
  ready_tasks: TaskItem[];
  upcoming_tasks: TaskItem[];
  pending_reviews: ReviewItem[];
  stats: { total_projects: number; completed_stages: number; total_cost_usd: number };
}

export default function PersonaDashboard() {
  const role = useRole();
  const navigate = useNavigate();
  const [data, setData] = useState<DashboardData | null>(null);
  const [archetypes, setArchetypes] = useState<Archetype[]>([]);
  // Engineer-initiated quick-create: clicking a card prompts only for a project
  // name, then creates with the archetype's default workflow (no type re-pick,
  // no workflow customization).
  const [createArchetype, setCreateArchetype] = useState<Archetype | null>(null);
  const [newName, setNewName] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  const submitCreate = async () => {
    if (!createArchetype || !newName.trim()) return;
    setCreating(true);
    setCreateError(null);
    try {
      const res = await api.post("/api/projects", {
        name: newName.trim(),
        archetype: createArchetype.slug,
        // omit workflow_ids → the archetype's default workflow template
      });
      const id = res.data?.id;
      setCreateArchetype(null);
      setNewName("");
      if (id) navigate(`/projects/${id}`);
    } catch (e) {
      setCreateError((e as Error).message || "Failed to create project.");
    }
    setCreating(false);
  };

  useEffect(() => {
    api.get("/api/dashboard", { params: { role } }).then((r) => setData(r.data)).catch(() => {});
    // DPE-CF and DPE-SA projects are initiated from the Product Workbench
    // (Request New / Ingest Existing), not as a manual quick-start from the
    // engineer side — hide both from the archetype tiles.
    api.get("/api/archetypes").then((r) => {
      const list = (r.data || []) as Archetype[];
      setArchetypes(list.filter((a) => a.slug !== "dpe-cf" && a.slug !== "dpe-sa"));
    }).catch(() => {});
  }, [role]);

  if (!data) return null;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 24, marginBottom: 32 }}>
      {/* Persona banner */}
      <div style={{ display: "flex", alignItems: "center", gap: 16, padding: "16px 20px", backgroundColor: "#1e293b", borderRadius: 12, color: "#fff" }}>
        <div style={{ width: 48, height: 48, borderRadius: "50%", backgroundColor: "#3b82f6", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 20, fontWeight: 700 }}>
          {role.charAt(0)}
        </div>
        <div>
          <div style={{ fontSize: 18, fontWeight: 700 }}>{role}</div>
          <div style={{ fontSize: 13, color: "#94a3b8" }}>{data.persona.description}</div>
        </div>
        <div style={{ marginLeft: "auto", display: "flex", gap: 20, fontSize: 13 }}>
          <div style={{ textAlign: "center" }}>
            <div style={{ fontSize: 22, fontWeight: 700 }}>{data.stats.total_projects}</div>
            <div style={{ color: "#94a3b8" }}>Projects</div>
          </div>
          <div style={{ textAlign: "center" }}>
            <div style={{ fontSize: 22, fontWeight: 700 }}>{data.stats.completed_stages}</div>
            <div style={{ color: "#94a3b8" }}>Completed</div>
          </div>
        </div>
      </div>

      {/* "Ready for You" / "Coming Up" intentionally omitted on the project
          landing — the engineer opens a project to see its next task. Cross-
          project / PO assignments still surface on the Incoming tab. */}

      {/* Create project buttons */}
      <div>
        <h3 style={{ margin: "0 0 12px", color: "#334155", fontSize: 15 }}>Create New Project</h3>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(240px, 1fr))", gap: 12 }}>
          {archetypes.map((a) => {
            // Data Modernization (dmod) is the PO-initiated product-building work
            // (started from the Product Workbench), not "coming soon" — mark it as
            // Data-Product-Owner-initiated and leave it non-clickable here.
            const poInitiated = a.slug === "dmod";
            const clickable = a.implemented;
            return (
            <div
              key={a.slug}
              onClick={() => { if (clickable) { setCreateArchetype(a); setNewName(""); setCreateError(null); } }}
              style={{
                padding: "24px 20px", borderRadius: 12,
                backgroundColor: clickable ? "#fff" : "#f8fafc",
                border: "1px solid #e2e8f0",
                cursor: clickable ? "pointer" : "default",
                opacity: clickable ? 1 : 0.7,
                transition: "all 0.15s",
                position: "relative",
              }}
            >
              {!a.implemented && (
                <span style={{ position: "absolute", top: 12, right: 12, fontSize: 10, fontWeight: 700,
                  color: poInitiated ? "#7c3aed" : "#94a3b8",
                  backgroundColor: poInitiated ? "#f3e8ff" : "#e2e8f0",
                  padding: "2px 8px", borderRadius: 8 }}>
                  {poInitiated ? "Product Owner" : "Coming Soon"}
                </span>
              )}
              <div style={{ fontSize: 17, fontWeight: 700, color: clickable ? "#1e293b" : "#64748b", marginBottom: 6 }}>
                {a.name}
              </div>
              <div style={{ fontSize: 13, color: "#64748b", lineHeight: 1.5 }}>
                {a.description}
              </div>
              {poInitiated && (
                <div style={{ fontSize: 12, color: "#7c3aed", marginTop: 10, fontWeight: 600 }}>
                  Started from the Product Workbench by a Data Product Owner.
                </div>
              )}
            </div>
            );
          })}
        </div>
      </div>

      {/* Quick-create: name-only prompt (skips the project-type + workflow wizard) */}
      {createArchetype && (
        <div
          onClick={() => { if (!creating) setCreateArchetype(null); }}
          style={{ position: "fixed", inset: 0, background: "rgba(15,23,42,0.45)", display: "flex",
            alignItems: "center", justifyContent: "center", zIndex: 1000 }}
        >
          <div onClick={(e) => e.stopPropagation()}
            style={{ background: "#fff", borderRadius: 10, padding: 24, width: 420, maxWidth: "92vw",
              boxShadow: "0 12px 40px rgba(0,0,0,0.25)" }}>
            <h3 style={{ margin: "0 0 4px", fontSize: 16, color: "#0f172a" }}>New {createArchetype.name} project</h3>
            <p style={{ margin: "0 0 16px", fontSize: 12.5, color: "#64748b" }}>{createArchetype.description}</p>
            <label style={{ display: "block", fontSize: 12, fontWeight: 600, color: "#334155", marginBottom: 4 }}>Project name</label>
            <input
              autoFocus
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") void submitCreate(); }}
              placeholder="e.g. Legacy Oracle → Snowflake"
              style={{ width: "100%", padding: "9px 11px", borderRadius: 6, border: "1px solid #cbd5e1",
                fontSize: 14, boxSizing: "border-box", marginBottom: 14 }}
            />
            {createError && <p style={{ fontSize: 12.5, color: "#dc2626", marginTop: 0 }}>{createError}</p>}
            <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
              <button onClick={() => { if (!creating) setCreateArchetype(null); }}
                style={{ padding: "8px 14px", borderRadius: 6, border: "1px solid #cbd5e1", background: "#fff",
                  color: "#475569", fontSize: 13, fontWeight: 600, cursor: "pointer" }}>Cancel</button>
              <button onClick={() => void submitCreate()} disabled={creating || !newName.trim()}
                style={{ padding: "8px 14px", borderRadius: 6, border: "none",
                  background: creating || !newName.trim() ? "#93c5fd" : "#2563eb", color: "#fff",
                  fontSize: 13, fontWeight: 600, cursor: creating || !newName.trim() ? "default" : "pointer" }}>
                {creating ? "Creating…" : "Create project"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
