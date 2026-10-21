import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import api from "../api/client";
import type { Archetype, WorkflowStep } from "../types";

interface WorkflowTemplate {
  workflow_id: string;
  name: string;
  description: string;
  order: number;
  repeatable: boolean;
  stage_count: number;
}

type Step = "archetype" | "workflow" | "settings";

export default function ConfigPanel() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [step, setStep] = useState<Step>("archetype");

  // Step 1: Archetype
  const [archetypes, setArchetypes] = useState<Archetype[]>([]);
  const [selectedArchetype, setSelectedArchetype] = useState<string>("");

  // Step 2: Workflow (multi-workflow model)
  const [workflowTemplates, setWorkflowTemplates] = useState<WorkflowTemplate[]>([]);
  const [selectedWorkflows, setSelectedWorkflows] = useState<Set<string>>(new Set());
  const [catalogItems, setCatalogItems] = useState<WorkflowTemplate[]>([]);
  const [catalogOpen, setCatalogOpen] = useState(false);

  // Legacy flat workflow (kept for backward compat with the validator endpoint)
  const [workflow, setWorkflow] = useState<WorkflowStep[]>([]);

  // Step 3: Project Settings — only project name. The data source connection
  // is now collected at runtime by the select_data_source stage at the head
  // of Data Discovery / Lineage Discovery, so the wizard no longer prompts
  // for it. Domain is also dropped: no engineer-runnable stage references
  // {domain} in its prompt template today (only reflect_on_reviews uses it,
  // and that stage is DPE-CF-only — DPE-CF projects arrive from the Product
  // Workbench with a domain already set).
  const [projectName, setProjectName] = useState("");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState("");

  // Load archetypes on mount. Hide DPE-CF and DPE-SA: those projects are
  // initiated from the Product Workbench (Request New / Ingest Existing),
  // not by an engineer creating a project manually. The archetypes themselves
  // stay in the backend registry — the ingest/request endpoints still
  // create dpe-cf / dpe-sa projects internally.
  useEffect(() => {
    api.get("/api/archetypes").then((res) => {
      const data = (res.data || []) as Archetype[];
      const visible = data.filter((a) => a.slug !== "dpe-cf" && a.slug !== "dpe-sa");
      setArchetypes(visible);
      const preselected = searchParams.get("archetype");
      if (preselected && preselected !== "dpe-cf" && preselected !== "dpe-sa") {
        handleArchetypeSelect(preselected);
      }
    }).catch(() => {});
  }, []);

  const handleArchetypeSelect = async (slug: string) => {
    setSelectedArchetype(slug);
    try {
      // Fetch multi-workflow templates
      const tmplRes = await api.get(`/api/workflow-catalog/archetype/${slug}`);
      const templates: WorkflowTemplate[] = tmplRes.data;
      setWorkflowTemplates(templates);
      setSelectedWorkflows(new Set(templates.map((t) => t.workflow_id)));

      // Also fetch the full catalog for the add-workflow modal
      const catRes = await api.get("/api/workflow-catalog");
      setCatalogItems(catRes.data);

      // Legacy flat workflow (still needed for backward compat)
      const wfRes = await api.get(`/api/archetypes/${slug}/workflow`);
      setWorkflow(wfRes.data.workflow);

      setStep("workflow");
    } catch {
      setError("Failed to load workflow template.");
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!projectName.trim()) {
      setError("Project name is required.");
      return;
    }
    setCreating(true);
    setError("");
    try {
      const payload = {
        name: projectName.trim(),
        archetype: selectedArchetype,
        workflow,
        workflow_ids: Array.from(selectedWorkflows),
      };
      const res = await api.post("/api/projects", payload);
      navigate(`/projects/${res.data.id}`);
    } catch (err: unknown) {
      const msg = (err as { response?: { data?: { detail?: string | { message?: string } } } })?.response?.data?.detail;
      setError(typeof msg === "string" ? msg : (msg as { message?: string })?.message || "Failed to create project.");
    }
    setCreating(false);
  };

  return (
    <div style={{ maxWidth: 640 }}>
      {/* Step indicator */}
      <div style={{ display: "flex", gap: 8, marginBottom: 24 }}>
        {(["archetype", "workflow", "settings"] as const).map((s, i) => (
          <div key={s} style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <div style={{
              width: 28, height: 28, borderRadius: "50%", display: "flex", alignItems: "center", justifyContent: "center",
              fontSize: 13, fontWeight: 700,
              backgroundColor: step === s ? "#3b82f6" : s === "archetype" || (s === "workflow" && selectedArchetype) || (s === "settings" && workflow.length > 0) ? "#22c55e" : "#e2e8f0",
              color: step === s || selectedArchetype ? "#fff" : "#94a3b8",
            }}>
              {i + 1}
            </div>
            <span style={{ fontSize: 13, fontWeight: step === s ? 700 : 400, color: step === s ? "#1e293b" : "#64748b" }}>
              {s === "archetype" ? "Project Type" : s === "workflow" ? "Workflow" : "Project Settings"}
            </span>
            {i < 2 && <span style={{ color: "#cbd5e1", margin: "0 4px" }}>—</span>}
          </div>
        ))}
      </div>

      {/* Step 1: Archetype selection */}
      {step === "archetype" && (
        <div>
          <h2 style={{ margin: "0 0 8px" }}>New Project</h2>
          <p style={{ color: "#64748b", fontSize: 14, margin: "0 0 20px" }}>Select a project type to get started.</p>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
            {archetypes.map((a) => (
              <div
                key={a.slug}
                onClick={() => a.implemented && handleArchetypeSelect(a.slug)}
                style={{
                  padding: 20, borderRadius: 10,
                  border: selectedArchetype === a.slug ? "2px solid #3b82f6" : "1px solid #e2e8f0",
                  backgroundColor: a.implemented ? "#fff" : "#f8fafc",
                  cursor: a.implemented ? "pointer" : "default",
                  opacity: a.implemented ? 1 : 0.6,
                  transition: "all 0.15s",
                  position: "relative",
                }}
              >
                {!a.implemented && (
                  <span style={{ position: "absolute", top: 10, right: 10, fontSize: 10, fontWeight: 700, color: "#94a3b8", backgroundColor: "#e2e8f0", padding: "2px 8px", borderRadius: 8 }}>
                    Coming Soon
                  </span>
                )}
                <div style={{ fontWeight: 700, fontSize: 16, color: a.implemented ? "#1e293b" : "#94a3b8", marginBottom: 6 }}>
                  {a.name}
                </div>
                <div style={{ fontSize: 13, color: "#64748b", lineHeight: 1.4 }}>
                  {a.description}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Step 2: Workflow customization */}
      {step === "workflow" && (
        <div>
          <h2 style={{ margin: "0 0 8px" }}>Customize Workflows</h2>
          <p style={{ color: "#64748b", fontSize: 14, margin: "0 0 16px" }}>
            Select which workflow groups to include in your project. You can add or remove workflows later.
          </p>

          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {workflowTemplates.map((tmpl) => {
              const isSelected = selectedWorkflows.has(tmpl.workflow_id);
              return (
                <div
                  key={tmpl.workflow_id}
                  style={{
                    display: "flex", alignItems: "center", justifyContent: "space-between",
                    padding: "14px 18px", borderRadius: 8,
                    backgroundColor: isSelected ? "#fff" : "#f8fafc",
                    border: isSelected ? "1px solid #e2e8f0" : "1px solid #f1f5f9",
                    opacity: isSelected ? 1 : 0.5,
                    transition: "all 0.15s",
                  }}
                >
                  <div style={{ flex: 1 }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                      <span style={{ fontWeight: 600, fontSize: 14 }}>{tmpl.name}</span>
                      <span style={{ fontSize: 11, color: "#94a3b8" }}>
                        {tmpl.stage_count} stage{tmpl.stage_count !== 1 ? "s" : ""}
                      </span>
                      {tmpl.repeatable && (
                        <span style={{ fontSize: 10, padding: "1px 6px", borderRadius: 4, backgroundColor: "#e0e7ff", color: "#4338ca", fontWeight: 600 }}>
                          Repeatable
                        </span>
                      )}
                    </div>
                    {tmpl.description && (
                      <div style={{ fontSize: 12, color: "#64748b", marginTop: 2 }}>{tmpl.description}</div>
                    )}
                  </div>
                  <label style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer", fontSize: 13 }}>
                    <input
                      type="checkbox"
                      checked={isSelected}
                      onChange={() => {
                        setSelectedWorkflows((prev) => {
                          const next = new Set(prev);
                          if (next.has(tmpl.workflow_id)) next.delete(tmpl.workflow_id);
                          else next.add(tmpl.workflow_id);
                          return next;
                        });
                      }}
                    />
                    {isSelected ? "Included" : "Excluded"}
                  </label>
                </div>
              );
            })}
          </div>

          {/* Add from catalog */}
          <button
            onClick={() => setCatalogOpen(true)}
            style={{
              marginTop: 12, padding: "8px 16px", borderRadius: 8,
              border: "2px dashed #cbd5e1", backgroundColor: "transparent",
              color: "#64748b", fontSize: 13, fontWeight: 600,
              cursor: "pointer", width: "100%", textAlign: "center",
            }}
          >
            + Add Workflow from Catalog
          </button>

          {catalogOpen && (
            <div style={{
              position: "fixed", inset: 0, backgroundColor: "rgba(0,0,0,0.4)",
              display: "flex", alignItems: "center", justifyContent: "center", zIndex: 1000,
            }} onClick={() => setCatalogOpen(false)}>
              <div
                style={{ backgroundColor: "#fff", borderRadius: 12, padding: 24, width: 480, maxHeight: "70vh", overflowY: "auto", boxShadow: "0 20px 60px rgba(0,0,0,0.2)" }}
                onClick={(e) => e.stopPropagation()}
              >
                <h3 style={{ margin: "0 0 16px", color: "#1e293b" }}>Workflow Catalog</h3>
                <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                  {catalogItems.filter((c) => !workflowTemplates.some((t) => t.workflow_id === c.workflow_id)).map((item) => {
                    const alreadyAdded = selectedWorkflows.has(item.workflow_id);
                    return (
                      <div key={item.workflow_id} style={{
                        padding: "12px 16px", borderRadius: 8,
                        border: `1px solid ${alreadyAdded ? "#bbf7d0" : "#e2e8f0"}`,
                        backgroundColor: alreadyAdded ? "#f0fdf4" : "#fff",
                      }}>
                        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                          <div>
                            <div style={{ fontWeight: 600, fontSize: 14, color: "#334155" }}>{item.name}</div>
                            {item.description && <div style={{ fontSize: 12, color: "#64748b", marginTop: 2 }}>{item.description}</div>}
                          </div>
                          {alreadyAdded ? (
                            <span style={{ fontSize: 12, color: "#16a34a", fontWeight: 600 }}>Added</span>
                          ) : (
                            <button onClick={() => {
                              setWorkflowTemplates((prev) => [...prev, item as WorkflowTemplate]);
                              setSelectedWorkflows((prev) => new Set([...prev, item.workflow_id]));
                            }} style={{
                              padding: "5px 14px", borderRadius: 6, border: "none",
                              backgroundColor: "#3b82f6", color: "#fff", fontSize: 12, fontWeight: 600, cursor: "pointer",
                            }}>Add</button>
                          )}
                        </div>
                      </div>
                    );
                  })}
                  {catalogItems.filter((c) => !workflowTemplates.some((t) => t.workflow_id === c.workflow_id)).length === 0 && (
                    <div style={{ color: "#94a3b8", fontSize: 13 }}>All available workflows are already included.</div>
                  )}
                </div>
                <button onClick={() => setCatalogOpen(false)} style={{ marginTop: 16, ...backBtnStyle }}>Close</button>
              </div>
            </div>
          )}

          <div style={{ display: "flex", gap: 8, marginTop: 20 }}>
            <button onClick={() => setStep("archetype")} style={backBtnStyle}>Back</button>
            <button
              onClick={() => setStep("settings")}
              disabled={selectedWorkflows.size === 0}
              style={{ ...primaryBtnStyle, opacity: selectedWorkflows.size === 0 ? 0.5 : 1 }}
            >
              Next
            </button>
          </div>
        </div>
      )}

      {/* Step 3: Project Settings — name only. Connection details are
          collected later by the select_data_source stage. */}
      {step === "settings" && (
        <form onSubmit={handleSubmit} style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <h2 style={{ margin: 0 }}>Project Settings</h2>
          <p style={{ margin: 0, color: "#64748b", fontSize: 13, lineHeight: 1.5 }}>
            Give the project a name. The data source connection is set later from inside the project,
            on the <strong>Select Data Source</strong> stage at the head of Data Discovery.
          </p>

          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <label style={labelStyle}>Project Name</label>
            <input
              style={inputStyle}
              value={projectName}
              onChange={(e) => setProjectName(e.target.value)}
              placeholder="e.g. Sales Performance Discovery"
              autoFocus
            />
          </div>

          {error && <div style={{ color: "#ef4444", fontSize: 13 }}>{error}</div>}

          <div style={{ display: "flex", gap: 8 }}>
            <button type="button" onClick={() => setStep("workflow")} style={backBtnStyle}>Back</button>
            <button type="submit" disabled={creating || !projectName.trim()} style={primaryBtnStyle}>
              {creating ? "Creating..." : "Create Project"}
            </button>
          </div>
        </form>
      )}
    </div>
  );
}

const inputStyle: React.CSSProperties = {
  padding: "8px 12px", borderRadius: 6, border: "1px solid #cbd5e1", fontSize: 14, width: "100%", boxSizing: "border-box",
};

const labelStyle: React.CSSProperties = { fontSize: 13, fontWeight: 600, color: "#334155", marginBottom: 4 };

const primaryBtnStyle: React.CSSProperties = {
  padding: "10px 24px", borderRadius: 8, border: "none", backgroundColor: "#3b82f6", color: "#fff", fontWeight: 700, fontSize: 15, cursor: "pointer",
};

const backBtnStyle: React.CSSProperties = {
  padding: "10px 24px", borderRadius: 8, border: "1px solid #cbd5e1", backgroundColor: "#fff", color: "#64748b", fontWeight: 600, fontSize: 15, cursor: "pointer",
};
