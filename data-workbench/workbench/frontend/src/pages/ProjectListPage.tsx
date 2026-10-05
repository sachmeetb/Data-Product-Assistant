import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import api from "../api/client";
import type { ProjectInfo } from "../types";
import StageChip from "../components/StageChip";
import PersonaDashboard from "../components/PersonaDashboard";
import { useConfirm } from "../components/dialogContext";

export default function ProjectListPage() {
  const [projects, setProjects] = useState<ProjectInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const confirm = useConfirm();

  const loadProjects = () => {
    api.get("/api/projects").then((res) => {
      setProjects(res.data);
      setLoading(false);
    });
  };

  useEffect(() => {
    loadProjects();
  }, []);

  const handleDelete = async (e: React.MouseEvent, projectId: number) => {
    e.preventDefault();
    e.stopPropagation();
    if (!(await confirm({
      title: "Delete project",
      message: "Delete this project and all its data?",
      confirmLabel: "Delete",
      tone: "danger",
    }))) return;
    try {
      await api.delete(`/api/projects/${projectId}`);
      setProjects((prev) => prev.filter((p) => p.id !== projectId));
    } catch (err) {
      console.error("Delete failed:", err);
    }
  };

  if (loading) return <div>Loading projects...</div>;

  return (
    <div>
      <PersonaDashboard />

      {projects.length > 0 && <h2 style={{ margin: "0 0 16px" }}>Projects</h2>}
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        {projects.map((p) => (
          <div
            key={p.id}
            style={{
              backgroundColor: "#fff",
              borderRadius: 8,
              border: "1px solid #e2e8f0",
              display: "flex",
              alignItems: "center",
            }}
          >
            <Link
              to={`/projects/${p.id}`}
              style={{
                textDecoration: "none",
                color: "inherit",
                padding: 16,
                flex: 1,
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
              }}
            >
              <div>
                <div style={{ fontWeight: 600, fontSize: 16 }}>{p.name}</div>
                <div style={{ fontSize: 13, color: "#64748b", marginTop: 4 }}>
                  {p.project_code} &middot; Created {new Date(p.created_at).toLocaleDateString()}
                </div>
              </div>
              <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
                {p.stages.map((s) => (
                  <StageChip key={s.stage_number} stage={s} showName />
                ))}
              </div>
            </Link>
            <button
              onClick={(e) => handleDelete(e, p.id)}
              title="Delete project"
              style={{
                padding: "8px 14px",
                marginRight: 12,
                borderRadius: 6,
                border: "1px solid #e2e8f0",
                backgroundColor: "#fff",
                color: "#94a3b8",
                fontSize: 13,
                cursor: "pointer",
              }}
              onMouseEnter={(e) => { e.currentTarget.style.color = "#ef4444"; e.currentTarget.style.borderColor = "#ef4444"; }}
              onMouseLeave={(e) => { e.currentTarget.style.color = "#94a3b8"; e.currentTarget.style.borderColor = "#e2e8f0"; }}
            >
              Delete
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}
