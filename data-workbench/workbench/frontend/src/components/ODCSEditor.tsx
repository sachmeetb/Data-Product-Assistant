import { useEffect, useState } from "react";
import api from "../api/client";
import { useCurrentUserEmail } from "../AuthContext";
import ProductChatPanel, { type AppliedSuggestion, type GuideMeRequest } from "./chat/ProductChatPanel";
import GuideMeButton from "./chat/GuideMeButton";

interface ODCSSpec {
  apiVersion?: string;
  kind?: string;
  id?: string;
  name?: string;
  version?: string;
  status?: string;
  domain?: string;
  dataProduct?: string;
  description?: string;
  purpose?: string;
  limitations?: string;
  tags?: string[];
  owners?: Array<{ username?: string; name?: string; role?: string; email?: string }>;
  stewards?: Array<{ name?: string; email?: string; role?: string; username?: string }>;
  team?: Array<{ username?: string; role?: string; name?: string; email?: string }>;
  roles?: Array<{ role?: string; description?: string; access?: string; datasets?: string[] }>;
  servers?: Array<{
    name?: string;
    environment?: string;
    type?: string;
    account?: string;
    database?: string;
    schema?: string;
    datasets?: unknown[];
  }>;
  support?: {
    contacts?: Array<{ type?: string; value?: string }>;
    documentation?: Array<{ type?: string; url?: string }>;
    escalationPolicy?: string;
  };
  customProperties?: Record<string, unknown>;
  extras?: Record<string, unknown>;
  schema?: Array<{
    name?: string;
    physicalName?: string;
    physicalType?: string;
    description?: string;
    properties?: Array<{
      name?: string;
      physicalName?: string;
      logicalName?: string;
      logicalType?: string;
      physicalType?: string;
      description?: string;
      primaryKey?: boolean;
      required?: boolean;
      criticalDataElement?: boolean;
      pii?: boolean;
      classification?: string;
      examples?: string[];
      logicalTypeOptions?: Record<string, unknown>;
    }>;
    foreignKeys?: unknown[];
  }>;
  quality?: Array<{
    rule?: string;
    name?: string;
    description?: string;
    severity?: string;
    dimension?: string;
    businessImpact?: string;
  }>;
  slaProperties?: Array<{ property?: string; value?: string; unit?: string }>;
  terms?: {
    usage?: string;
    limitations?: string;
    billing?: string;
    noticePeriod?: string;
  };
}

interface Template {
  name: string;
  filename: string;
  domain: string | null;
  description: string;
}

type Tab = "info" | "schema" | "quality" | "sla" | "terms" | "team" | "roles" | "servers" | "yaml";

// Curated value sets for the richer form controls. Rendered as <select>s whose
// options are the known set UNION the current value, so a spec carrying a
// non-standard value never loses it. Vocabulary mirrors `lib/ruleCategory.ts`.
const RULE_TYPES = ["notNull", "unique", "allowedValues", "range", "regex", "maxLength", "referentialIntegrity"];
const SEVERITIES = ["error", "warning", "info"];
const DIMENSIONS = ["completeness", "uniqueness", "validity", "accuracy", "consistency", "timeliness", "integrity"];
// ODCS v3.1 logical types — the platform-independent shape the blueprint declares.
const LOGICAL_TYPES = ["string", "integer", "number", "boolean", "date", "timestamp", "object", "array"];

// Option list guided by a known set but never dropping a non-standard current value.
const withCurrent = (list: string[], cur?: string) =>
  (cur && !list.includes(cur) ? [cur, ...list] : list);

interface Props {
  projectId?: number;
  stageNumber?: number;
  onSaved?: () => void;
  /** Base URL the editor loads/saves/exports against. Defaults to the
   *  project ODCS route. The Blueprint Library passes a template route
   *  (`/api/templates/{id}`) so the SAME editor authors project contracts
   *  AND standalone spec templates without a fork. */
  apiBase?: string;
  /** "project" (default) exposes dprod/stage-complete/materialise; "template"
   *  hides those project-only actions and swaps Materialise → Export. */
  mode?: "project" | "template";
}

export default function ODCSEditor({ projectId, stageNumber, onSaved, apiBase, mode = "project" }: Props) {
  const base = apiBase ?? `/api/projects/${projectId}/odcs`;
  const isTemplate = mode === "template";
  const [spec, setSpec] = useState<ODCSSpec | null>(null);
  const [activeTab, setActiveTab] = useState<Tab>("info");
  const [templates, setTemplates] = useState<Template[]>([]);
  const [showTemplates, setShowTemplates] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [yamlPreview, setYamlPreview] = useState("");
  const [completing, setCompleting] = useState(false);
  const [importMode, setImportMode] = useState(false);
  const [importText, setImportText] = useState("");
  const [importError, setImportError] = useState<string | null>(null);
  const [importing, setImporting] = useState(false);

  // ── AI assist (template mode) — reuses the Product Authoring chat drawer +
  //    the shared suggestion/Apply protocol, backed by the
  //    `template-authoring-assistant` skill (surface="template"). Suggestions
  //    apply straight onto the editor's own spec state via updateSpec.
  const ownerEmail = useCurrentUserEmail();
  const [chatOpen, setChatOpen] = useState(false);
  const [chatRequest, setChatRequest] = useState<GuideMeRequest | null>(null);

  const openAssist = (prefill = "", autoSend = false) => {
    setChatRequest({ prefill, autoSend, nonce: Date.now() });
    setChatOpen(true);
  };

  const templateContext = (): Record<string, unknown> | null => {
    if (!spec) return null;
    const props = spec.schema?.[0]?.properties || [];
    return {
      surface: "template",
      template_id: spec.id || "",
      name: spec.name || "",
      domain: spec.domain || "",
      description: spec.description || "",
      purpose: spec.purpose || "",
      product_kind: (spec.customProperties?.productKind as string) || spec.dataProduct || "",
      dataset_name: spec.schema?.[0]?.physicalName || "",
      columns: props.map((p) => ({
        name: p.name,
        physical_type: p.physicalType || p.logicalType || "",
        description: p.description || "",
        primary_key: !!p.primaryKey,
        required: !!p.required,
      })),
    };
  };

  const applyTemplateSuggestion = (s: AppliedSuggestion): string => {
    if (!spec) return "No spec loaded";
    switch (s.applies_to) {
      case "name":
        updateSpec("name", s.value || "");
        return "Name updated";
      case "domain":
        updateSpec("domain", s.value || "");
        return "Domain updated";
      case "description":
        updateSpec("description", s.value || "");
        return "Description updated";
      case "purpose":
        updateSpec("purpose", s.value || "");
        return "Purpose updated";
      case "dataset_name":
        updateSpec("schema.0.physicalName", s.value || "");
        return "Dataset name updated";
      case "schema_add_columns": {
        const additions = (s.columns || []).map((c) => ({
          name: c.name,
          physicalName: c.name,
          logicalName: "",
          logicalType: c.logical_type || "",
          physicalType: c.physical_type || c.logical_type || "",
          description: c.description || "",
          primaryKey: !!c.primary_key,
          required: false,
          criticalDataElement: false,
        }));
        if (additions.length === 0) return "No columns to add";
        const schemas = JSON.parse(JSON.stringify(spec.schema || [])) as NonNullable<ODCSSpec["schema"]>;
        if (schemas.length === 0) {
          schemas.push({
            name: spec.name || "dataset",
            physicalName: "dataset",
            physicalType: "table",
            description: "",
            properties: [],
          });
        }
        schemas[0].properties = [...(schemas[0].properties || []), ...additions];
        updateSpec("schema", schemas);
        return `Added ${additions.length} column(s)`;
      }
      case "rule_create": {
        // Map the shared chat rule payload onto ODCS `quality[]` entries. The
        // column is carried in the description as a `[column=X]` hint (the ODCS
        // canonicaliser convention) since the quality row has no column field.
        const additions = (s.rules || []).map((r) => {
          const p = (r.params || {}) as Record<string, unknown>;
          const bits: string[] = [];
          if (Array.isArray(p.values)) bits.push(`values: ${p.values.map(String).join(", ")}`);
          if (p.min != null || p.max != null) bits.push(`range: ${String(p.min ?? "")}..${String(p.max ?? "")}`);
          if (typeof p.pattern === "string" && p.pattern) bits.push(`pattern: ${p.pattern}`);
          if (p.maxLength != null) bits.push(`maxLength: ${String(p.maxLength)}`);
          const base = (r.description || `${r.column} ${r.rule_type}`).trim();
          const hint = [r.column ? `column=${r.column}` : "", bits.join("; ")].filter(Boolean).join(", ");
          return {
            rule: r.rule_type || "",
            name: (r.description || `${r.column}: ${r.rule_type}`).slice(0, 60),
            description: hint ? `${base} [${hint}]` : base,
            severity: r.severity || "warning",
            dimension: "",
            businessImpact: "",
          };
        });
        if (additions.length === 0) return "No rules to add";
        updateSpec("quality", [...(spec.quality || []), ...additions]);
        return `Added ${additions.length} quality rule(s)`;
      }
      default:
        return "Not applicable to a template";
    }
  };

  // Load existing spec or show template picker
  useEffect(() => {
    api.get(base).then((r) => {
      if (r.data.spec) {
        setSpec(r.data.spec);
      } else {
        setShowTemplates(true);
      }
    }).catch(() => setShowTemplates(true));

    // The clone-from picker is a project-authoring convenience; a template is
    // always loaded directly, so skip it in template mode.
    if (!isTemplate) {
      api.get("/api/odcs/templates").then((r) => setTemplates(r.data)).catch(() => {});
    }
  }, [base, isTemplate]);

  const loadTemplate = async (filename: string) => {
    const r = await api.get(`/api/odcs/templates/${filename}`);
    setSpec(r.data.spec);
    setShowTemplates(false);
  };

  const handleFilePick = (file: File) => {
    const reader = new FileReader();
    reader.onload = () => {
      setImportText(typeof reader.result === "string" ? reader.result : "");
      setImportError(null);
    };
    reader.onerror = () => setImportError("Could not read file");
    reader.readAsText(file);
  };

  const handleImport = async () => {
    if (!importText.trim()) return;
    setImporting(true);
    setImportError(null);
    try {
      const r = await api.post("/api/odcs/parse", { yaml: importText });
      setSpec(r.data.spec);
      setShowTemplates(false);
      setImportMode(false);
      setImportText("");
    } catch (e: unknown) {
      const detail =
        (e as { response?: { data?: { detail?: string } }; message?: string })
          ?.response?.data?.detail ??
        (e instanceof Error ? e.message : "Import failed");
      setImportError(String(detail));
    }
    setImporting(false);
  };

  const startBlank = () => {
    setSpec({
      apiVersion: "v3.1.0",
      kind: "DataContract",
      id: "",
      name: "",
      version: "1.0.0",
      status: "draft",
      domain: "",
      dataProduct: "",
      description: "",
      purpose: "",
      limitations: "",
      tags: [],
      owners: [{ username: "", name: "", role: "", email: "" }],
      stewards: [],
      team: [],
      roles: [],
      servers: [],
      schema: [],
      quality: [],
      slaProperties: [],
      terms: { usage: "", limitations: "", billing: "", noticePeriod: "" },
    });
    setShowTemplates(false);
  };

  const handleSave = async () => {
    if (!spec) return;
    setSaving(true);
    try {
      await api.put(base, { spec });
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
      onSaved?.();
    } catch (e) {
      console.error("Save failed", e);
    }
    setSaving(false);
  };

  const handleMaterialise = async () => {
    try {
      // Template mode has no graph-materialise route — it exports the stored
      // ODCS directly; project mode materialises the synthesised spec.
      const r = await api.get(`${base}/${isTemplate ? "export" : "materialise"}`);
      setYamlPreview(r.data.yaml);
      setActiveTab("yaml");
    } catch {
      setYamlPreview("# No spec saved to graph yet. Save first.");
      setActiveTab("yaml");
    }
  };

  const handleMarkComplete = async () => {
    if (!stageNumber) return;
    setCompleting(true);
    try {
      await handleSave();
      await api.post(`/api/projects/${projectId}/stages/${stageNumber}/complete`);
      onSaved?.();
    } catch (e) {
      console.error("Complete failed", e);
    }
    setCompleting(false);
  };

  // Field update helper — auto-creates intermediate objects/arrays so the form
  // can edit paths like "owners.0.name" even when the imported spec omits them.
  const updateSpec = (path: string, value: unknown) => {
    if (!spec) return;
    const parts = path.split(".");
    const newSpec = JSON.parse(JSON.stringify(spec));
    let obj: unknown = newSpec;
    for (let i = 0; i < parts.length - 1; i++) {
      const key = parts[i];
      const nextIsIndex = /^\d+$/.test(parts[i + 1]);
      if (/^\d+$/.test(key)) {
        const arr = obj as unknown[];
        const idx = parseInt(key);
        if (arr[idx] === undefined || arr[idx] === null) {
          arr[idx] = nextIsIndex ? [] : {};
        }
        obj = arr[idx];
      } else {
        const rec = obj as Record<string, unknown>;
        if (rec[key] === undefined || rec[key] === null) {
          rec[key] = nextIsIndex ? [] : {};
        }
        obj = rec[key];
      }
    }
    const last = parts[parts.length - 1];
    if (/^\d+$/.test(last)) {
      (obj as unknown[])[parseInt(last)] = value;
    } else {
      (obj as Record<string, unknown>)[last] = value;
    }
    setSpec(newSpec);
  };

  // Template picker
  if (showTemplates) {
    if (importMode) {
      return (
        <div style={styles.container}>
          <h3 style={styles.heading}>Import ODCS YAML</h3>
          <p style={styles.subtext}>
            Upload a <code>.yaml</code> file or paste an ODCS specification. The
            parsed contract will pre-populate the editor for you to review
            before saving.
          </p>
          <div style={styles.importPanel}>
            <label style={styles.fieldLabel}>Upload file</label>
            <input
              type="file"
              accept=".yaml,.yml,text/yaml,text/plain"
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) handleFilePick(f);
              }}
              style={{ fontSize: 13 }}
            />
            <label style={{ ...styles.fieldLabel, marginTop: 14 }}>
              Or paste YAML
            </label>
            <textarea
              value={importText}
              onChange={(e) => {
                setImportText(e.target.value);
                setImportError(null);
              }}
              placeholder="Paste ODCS YAML here..."
              rows={14}
              style={{ ...styles.fieldInput, fontFamily: "'Fira Code', monospace", fontSize: 12 }}
            />
            {importError && <div style={styles.importError}>{importError}</div>}
            <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
              <button
                onClick={handleImport}
                disabled={importing || !importText.trim()}
                style={{
                  ...styles.primaryBtn,
                  opacity: importing || !importText.trim() ? 0.6 : 1,
                  cursor: importing || !importText.trim() ? "not-allowed" : "pointer",
                }}
              >
                {importing ? "Loading..." : "Load"}
              </button>
              <button
                onClick={() => {
                  setImportMode(false);
                  setImportText("");
                  setImportError(null);
                }}
                style={styles.secondaryBtn}
              >
                Back
              </button>
            </div>
          </div>
        </div>
      );
    }
    return (
      <div style={styles.container}>
        <h3 style={styles.heading}>Create ODCS Specification</h3>
        <p style={styles.subtext}>Choose a starting point for your data contract.</p>
        <div style={styles.templateGrid}>
          <div onClick={startBlank} style={styles.templateCard}>
            <div style={styles.templateIcon}>+</div>
            <div style={styles.templateName}>Blank Contract</div>
            <div style={styles.templateDesc}>Start from scratch</div>
          </div>
          <div onClick={() => setImportMode(true)} style={styles.templateCard}>
            <div style={styles.templateIcon}>↑</div>
            <div style={styles.templateName}>Import from YAML</div>
            <div style={styles.templateDesc}>Upload or paste an existing ODCS contract</div>
          </div>
          {templates.map((t) => (
            <div key={t.filename} onClick={() => loadTemplate(t.filename)} style={styles.templateCard}>
              <div style={styles.templateIcon}>D</div>
              <div style={styles.templateName}>{t.name}</div>
              <div style={styles.templateDesc}>{t.description?.slice(0, 80) || t.filename}</div>
            </div>
          ))}
        </div>
      </div>
    );
  }

  if (!spec) return <div style={{ color: "#94a3b8" }}>Loading...</div>;

  const tabs: { key: Tab; label: string }[] = [
    { key: "info", label: "Info" },
    { key: "schema", label: "Schema" },
    { key: "quality", label: "Quality" },
    { key: "sla", label: "SLA" },
    { key: "terms", label: "Terms" },
    { key: "team", label: "Team" },
    { key: "roles", label: "Roles" },
    { key: "servers", label: "Servers" },
    { key: "yaml", label: "YAML" },
  ];

  return (
    <div style={styles.container}>
      {/* Header */}
      <div style={styles.header}>
        <div>
          <h3 style={styles.heading}>
            {spec.name || "Untitled Contract"}
            <span style={styles.statusBadge}>{spec.status || "draft"}</span>
          </h3>
        </div>
        <div style={styles.headerActions}>
          {isTemplate && (
            <button
              onClick={() => openAssist("")}
              style={{ ...styles.secondaryBtn, borderColor: "#7c3aed", color: "#7c3aed", fontWeight: 700 }}
              title="Ask the AI assistant to help author this template"
            >
              ✨ Assist
            </button>
          )}
          <button onClick={handleMaterialise} style={styles.secondaryBtn}>{isTemplate ? "Export YAML" : "Materialise YAML"}</button>
          <button onClick={handleSave} disabled={saving} style={styles.primaryBtn}>
            {saving ? "Saving..." : saved ? "Saved!" : "Save Draft"}
          </button>
          {stageNumber && (
            <button onClick={handleMarkComplete} disabled={completing} style={{ ...styles.primaryBtn, backgroundColor: "#22c55e" }}>
              {completing ? "Completing..." : "Save & Complete Stage"}
            </button>
          )}
        </div>
      </div>

      {/* Tabs */}
      <div style={styles.tabBar}>
        {tabs.map((t) => (
          <button
            key={t.key}
            onClick={() => t.key === "yaml" ? handleMaterialise() : setActiveTab(t.key)}
            style={{
              ...styles.tab,
              borderBottomColor: activeTab === t.key ? "#3b82f6" : "transparent",
              color: activeTab === t.key ? "#1e293b" : "#64748b",
              fontWeight: activeTab === t.key ? 700 : 500,
            }}
          >
            {t.label}
          </button>
        ))}
      </div>

      {/* Tab content */}
      <div style={styles.tabContent}>
        {activeTab === "info" && (
          <div style={styles.formGrid}>
            {isTemplate && (
              <div style={{ gridColumn: "1 / -1", display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center", padding: "4px 0 8px" }}>
                <span style={{ fontSize: 12, color: "#64748b", fontWeight: 600 }}>✨ AI assist:</span>
                <GuideMeButton label="Improve description" onClick={() => openAssist("Improve the description for this data-product template — a crisp, consumer-facing summary of what it is and who uses it.", true)} />
                <GuideMeButton label="Draft a purpose" onClick={() => openAssist("Draft a clear purpose statement for this template (business outcome it enables).", true)} />
                <GuideMeButton label="Suggest missing columns" onClick={() => openAssist("Suggest columns this template is likely missing for its domain, and return them as a schema_add_columns suggestion.", true)} />
                <GuideMeButton label="Ask anything" onClick={() => openAssist("")} />
              </div>
            )}
            <Field label="Contract ID" value={spec.id || ""} onChange={(v) => updateSpec("id", v)} />
            <Field label="Name" value={spec.name || ""} onChange={(v) => updateSpec("name", v)} />
            <Field label="Version" value={spec.version || ""} onChange={(v) => updateSpec("version", v)} />
            <Field
              label="Status"
              value={spec.status || ""}
              onChange={(v) => updateSpec("status", v)}
              readOnly={isTemplate}
              hint={isTemplate ? "The blueprint's lifecycle (draft → published). Managed by the Publish / Retract buttons — not edited here." : undefined}
            />
            <Field label="Domain" value={spec.domain || ""} onChange={(v) => updateSpec("domain", v)} />
            <Field
              label={isTemplate ? "Sub-domain (optional)" : "Data Product"}
              value={spec.dataProduct || ""}
              onChange={(v) => updateSpec("dataProduct", v)}
              hint={isTemplate ? "A finer grouping within the domain (e.g. 'Cards → Debit'). Used only for filtering in the library; optional." : undefined}
            />
            <Field label="Description" value={spec.description || ""} onChange={(v) => updateSpec("description", v)} wide multiline />
            <Field label="Purpose" value={spec.purpose || ""} onChange={(v) => updateSpec("purpose", v)} wide multiline />
            <Field label="Limitations" value={spec.limitations || ""} onChange={(v) => updateSpec("limitations", v)} wide multiline />
            <div style={styles.sectionDivider}>
              Primary Owner
              {spec.owners && spec.owners.length > 1 && (
                <span style={{ fontWeight: 400, fontSize: 11, color: "#94a3b8", marginLeft: 8 }}>
                  ({spec.owners.length - 1} additional owner{spec.owners.length - 1 === 1 ? "" : "s"} preserved on save)
                </span>
              )}
            </div>
            <Field label="Name" value={spec.owners?.[0]?.name || ""} onChange={(v) => updateSpec("owners.0.name", v)} />
            <Field label="Role" value={spec.owners?.[0]?.role || ""} onChange={(v) => updateSpec("owners.0.role", v)} />
            <Field label="Email" value={spec.owners?.[0]?.email || ""} onChange={(v) => updateSpec("owners.0.email", v)} />
            <Field label="Username" value={spec.owners?.[0]?.username || ""} onChange={(v) => updateSpec("owners.0.username", v)} />

            <div style={styles.sectionDivider}>Primary Steward
              {spec.stewards && spec.stewards.length > 1 && (
                <span style={{ fontWeight: 400, fontSize: 11, color: "#94a3b8", marginLeft: 8 }}>
                  ({spec.stewards.length - 1} additional steward{spec.stewards.length - 1 === 1 ? "" : "s"} preserved on save)
                </span>
              )}
            </div>
            <Field label="Name" value={spec.stewards?.[0]?.name || ""} onChange={(v) => updateSpec("stewards.0.name", v)} />
            <Field label="Role" value={spec.stewards?.[0]?.role || ""} onChange={(v) => updateSpec("stewards.0.role", v)} />
            <Field label="Email" value={spec.stewards?.[0]?.email || ""} onChange={(v) => updateSpec("stewards.0.email", v)} />

            <div style={styles.sectionDivider}>Tags</div>
            <Field
              label="Tags (comma-separated)"
              value={(spec.tags || []).join(", ")}
              onChange={(v) => updateSpec("tags", v.split(",").map((t) => t.trim()).filter(Boolean))}
              wide
            />
          </div>
        )}

        {activeTab === "schema" && (
          <div>
            {(spec.schema || []).map((schema, si) => (
              <div key={si} style={styles.schemaBlock}>
                <div style={styles.schemaHeader}>
                  <Field label="Table Name" value={schema.name || ""} onChange={(v) => updateSpec(`schema.${si}.name`, v)} />
                  <Field label="Physical Name" value={schema.physicalName || ""} onChange={(v) => updateSpec(`schema.${si}.physicalName`, v)} />
                  <Field label="Physical Type" value={schema.physicalType || ""} onChange={(v) => updateSpec(`schema.${si}.physicalType`, v)} />
                  <Field label="Description" value={schema.description || ""} onChange={(v) => updateSpec(`schema.${si}.description`, v)} wide />
                </div>
                <table style={styles.colTable}>
                  <thead>
                    <tr>
                      <th style={styles.colTh}>Name</th>
                      <th style={styles.colTh}>Logical Name</th>
                      <th style={styles.colTh}>Logical Type</th>
                      <th style={styles.colTh}>
                        Physical Type
                        {isTemplate && (
                          <span style={{ fontWeight: 400, textTransform: "none", color: "#94a3b8", marginLeft: 4 }}>
                            (set at implementation)
                          </span>
                        )}
                      </th>
                      <th style={styles.colTh}>Description</th>
                      <th style={styles.colTh}>PK</th>
                      <th style={styles.colTh}>Required</th>
                      <th style={styles.colTh}>CDE</th>
                      <th style={styles.colTh}></th>
                    </tr>
                  </thead>
                  <tbody>
                    {(schema.properties || []).map((prop, pi) => (
                      <tr key={pi}>
                        <td style={styles.colTd}>
                          <input value={prop.name || ""} onChange={(e) => updateSpec(`schema.${si}.properties.${pi}.name`, e.target.value)} style={styles.cellInput} />
                        </td>
                        <td style={styles.colTd}>
                          <input value={prop.logicalName || ""} onChange={(e) => updateSpec(`schema.${si}.properties.${pi}.logicalName`, e.target.value)} style={styles.cellInput} />
                        </td>
                        <td style={styles.colTd}>
                          <select
                            value={prop.logicalType || ""}
                            onChange={(e) => updateSpec(`schema.${si}.properties.${pi}.logicalType`, e.target.value)}
                            style={styles.cellInput}
                          >
                            <option value="">—</option>
                            {withCurrent(LOGICAL_TYPES, prop.logicalType).map((t) => <option key={t} value={t}>{t}</option>)}
                          </select>
                        </td>
                        <td style={styles.colTd}>
                          {isTemplate ? (
                            <input
                              value={prop.physicalType || ""}
                              readOnly
                              disabled
                              title="Physical type is an implementation detail — chosen when the product is built on a specific platform, not on the blueprint."
                              style={{ ...styles.cellInput, background: "#f1f5f9", color: "#94a3b8", cursor: "not-allowed" }}
                            />
                          ) : (
                            <input value={prop.physicalType || ""} onChange={(e) => updateSpec(`schema.${si}.properties.${pi}.physicalType`, e.target.value)} style={styles.cellInput} />
                          )}
                        </td>
                        <td style={styles.colTd}>
                          <input value={prop.description || ""} onChange={(e) => updateSpec(`schema.${si}.properties.${pi}.description`, e.target.value)} style={{ ...styles.cellInput, minWidth: 200 }} />
                        </td>
                        <td style={styles.colTd}>
                          <input type="checkbox" checked={prop.primaryKey || false} onChange={(e) => updateSpec(`schema.${si}.properties.${pi}.primaryKey`, e.target.checked)} />
                        </td>
                        <td style={styles.colTd}>
                          <input type="checkbox" checked={prop.required || false} onChange={(e) => updateSpec(`schema.${si}.properties.${pi}.required`, e.target.checked)} />
                        </td>
                        <td style={styles.colTd}>
                          <input type="checkbox" checked={prop.criticalDataElement || false} onChange={(e) => updateSpec(`schema.${si}.properties.${pi}.criticalDataElement`, e.target.checked)} />
                        </td>
                        <td style={styles.colTd}>
                          <button
                            type="button"
                            title="Remove this attribute"
                            aria-label={`Remove attribute ${prop.name || pi + 1}`}
                            onClick={() => {
                              const props = (schema.properties || []).filter((_, idx) => idx !== pi);
                              updateSpec(`schema.${si}.properties`, props);
                            }}
                            style={styles.rowRemoveBtn}
                          >
                            ✕
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <button
                  onClick={() => {
                    const props = [...(schema.properties || []), { name: "", physicalName: "", logicalName: "", logicalType: "", physicalType: "", description: "", primaryKey: false, required: false, criticalDataElement: false }];
                    updateSpec(`schema.${si}.properties`, props);
                  }}
                  style={styles.addBtn}
                >
                  + Add Property
                </button>
                <button
                  type="button"
                  onClick={() => {
                    const schemas = (spec.schema || []).filter((_, idx) => idx !== si);
                    updateSpec("schema", schemas);
                  }}
                  style={styles.removeSchemaBtn}
                >
                  Remove table
                </button>
              </div>
            ))}
            <button
              onClick={() => {
                const schemas = [...(spec.schema || []), { name: "", physicalName: "", physicalType: "table", description: "", properties: [] }];
                updateSpec("schema", schemas);
              }}
              style={styles.addBtn}
            >
              + Add Table
            </button>
          </div>
        )}

        {activeTab === "quality" && (
          <div>
            {isTemplate && (
              <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center", padding: "4px 0 12px" }}>
                <span style={{ fontSize: 12, color: "#64748b", fontWeight: 600 }}>✨ AI assist:</span>
                <GuideMeButton
                  label="Recommend rules"
                  onClick={() => openAssist("Recommend data-quality rules for this data product's columns — grounded in the schema, column descriptions, and the domain catalog. Cover keys (not-null + unique), status/type columns (allowed values), amounts (non-negative ranges), and identifier formats. Return them as a rule_create suggestion.", true)}
                />
              </div>
            )}
            {(spec.quality || []).map((q, qi) => (
              <div key={qi} style={styles.qualityRow}>
                <Field label="Rule" value={q.rule || ""} onChange={(v) => updateSpec(`quality.${qi}.rule`, v)} options={RULE_TYPES} />
                <Field label="Name" value={q.name || ""} onChange={(v) => updateSpec(`quality.${qi}.name`, v)} />
                <Field label="Severity" value={q.severity || ""} onChange={(v) => updateSpec(`quality.${qi}.severity`, v)} radios={SEVERITIES} />
                <Field label="Dimension" value={q.dimension || ""} onChange={(v) => updateSpec(`quality.${qi}.dimension`, v)} options={DIMENSIONS} />
                <Field label="Description" value={q.description || ""} onChange={(v) => updateSpec(`quality.${qi}.description`, v)} wide multiline />
                <Field label="Business Impact" value={q.businessImpact || ""} onChange={(v) => updateSpec(`quality.${qi}.businessImpact`, v)} wide />
              </div>
            ))}
            <button
              onClick={() => updateSpec("quality", [...(spec.quality || []), { rule: "", name: "", description: "", severity: "warning", dimension: "", businessImpact: "" }])}
              style={styles.addBtn}
            >
              + Add Quality Rule
            </button>
          </div>
        )}

        {activeTab === "sla" && (
          <div style={styles.formGrid}>
            {(spec.slaProperties || []).map((s, si) => (
              <div key={si} style={{ display: "flex", gap: 12, alignItems: "flex-end" }}>
                <Field label="Property" value={s.property || ""} onChange={(v) => updateSpec(`slaProperties.${si}.property`, v)} />
                <Field label="Value" value={s.value || ""} onChange={(v) => updateSpec(`slaProperties.${si}.value`, v)} />
                <Field label="Unit" value={s.unit || ""} onChange={(v) => updateSpec(`slaProperties.${si}.unit`, v)} />
              </div>
            ))}
            <button
              onClick={() => updateSpec("slaProperties", [...(spec.slaProperties || []), { property: "", value: "", unit: "" }])}
              style={styles.addBtn}
            >
              + Add SLA
            </button>
          </div>
        )}

        {activeTab === "terms" && (
          <div style={styles.formGrid}>
            <Field label="Usage" value={spec.terms?.usage || ""} onChange={(v) => updateSpec("terms.usage", v)} wide multiline />
            <Field label="Limitations" value={spec.terms?.limitations || ""} onChange={(v) => updateSpec("terms.limitations", v)} wide multiline />
            <Field label="Billing" value={spec.terms?.billing || ""} onChange={(v) => updateSpec("terms.billing", v)} wide />
            <Field label="Notice Period" value={spec.terms?.noticePeriod || ""} onChange={(v) => updateSpec("terms.noticePeriod", v)} />
          </div>
        )}

        {activeTab === "team" && (
          <div>
            <table style={styles.colTable}>
              <thead>
                <tr>
                  <th style={styles.colTh}>Username</th>
                  <th style={styles.colTh}>Name</th>
                  <th style={styles.colTh}>Role</th>
                  <th style={styles.colTh}>Email</th>
                </tr>
              </thead>
              <tbody>
                {(spec.team || []).map((tm, ti) => (
                  <tr key={ti}>
                    <td style={styles.colTd}>
                      <input value={tm.username || ""} onChange={(e) => updateSpec(`team.${ti}.username`, e.target.value)} style={styles.cellInput} />
                    </td>
                    <td style={styles.colTd}>
                      <input value={tm.name || ""} onChange={(e) => updateSpec(`team.${ti}.name`, e.target.value)} style={styles.cellInput} />
                    </td>
                    <td style={styles.colTd}>
                      <input value={tm.role || ""} onChange={(e) => updateSpec(`team.${ti}.role`, e.target.value)} style={styles.cellInput} />
                    </td>
                    <td style={styles.colTd}>
                      <input value={tm.email || ""} onChange={(e) => updateSpec(`team.${ti}.email`, e.target.value)} style={styles.cellInput} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <button
              onClick={() => updateSpec("team", [...(spec.team || []), { username: "", role: "", name: "", email: "" }])}
              style={styles.addBtn}
            >
              + Add Team Member
            </button>
          </div>
        )}

        {activeTab === "roles" && (
          <div>
            {(spec.roles || []).map((r, ri) => (
              <div key={ri} style={styles.qualityRow}>
                <Field label="Role" value={r.role || ""} onChange={(v) => updateSpec(`roles.${ri}.role`, v)} />
                <Field label="Access" value={r.access || ""} onChange={(v) => updateSpec(`roles.${ri}.access`, v)} />
                <Field label="Description" value={r.description || ""} onChange={(v) => updateSpec(`roles.${ri}.description`, v)} wide multiline />
                <Field
                  label="Datasets (comma-separated)"
                  value={(r.datasets || []).join(", ")}
                  onChange={(v) => updateSpec(`roles.${ri}.datasets`, v.split(",").map((d) => d.trim()).filter(Boolean))}
                  wide
                />
              </div>
            ))}
            <button
              onClick={() => updateSpec("roles", [...(spec.roles || []), { role: "", description: "", access: "", datasets: [] }])}
              style={styles.addBtn}
            >
              + Add Role
            </button>
          </div>
        )}

        {activeTab === "servers" && (
          <div>
            {(spec.servers || []).map((sv, svi) => (
              <div key={svi} style={styles.schemaBlock}>
                <div style={styles.schemaHeader}>
                  <Field label="Name" value={sv.name || ""} onChange={(v) => updateSpec(`servers.${svi}.name`, v)} />
                  <Field label="Environment" value={sv.environment || ""} onChange={(v) => updateSpec(`servers.${svi}.environment`, v)} />
                  <Field label="Type" value={sv.type || ""} onChange={(v) => updateSpec(`servers.${svi}.type`, v)} />
                  <Field label="Account" value={sv.account || ""} onChange={(v) => updateSpec(`servers.${svi}.account`, v)} />
                  <Field label="Database" value={sv.database || ""} onChange={(v) => updateSpec(`servers.${svi}.database`, v)} />
                  <Field label="Schema" value={sv.schema || ""} onChange={(v) => updateSpec(`servers.${svi}.schema`, v)} />
                </div>
                {sv.datasets && sv.datasets.length > 0 && (
                  <div style={{ marginTop: 10 }}>
                    <label style={styles.fieldLabel}>Datasets (preserved on save)</label>
                    <pre style={{ margin: 0, padding: 10, backgroundColor: "#f1f5f9", borderRadius: 6, fontSize: 11, color: "#334155", maxHeight: 200, overflow: "auto" }}>
                      {JSON.stringify(sv.datasets, null, 2)}
                    </pre>
                  </div>
                )}
              </div>
            ))}
            <button
              onClick={() => updateSpec("servers", [...(spec.servers || []), { name: "", environment: "", type: "", account: "", database: "", schema: "", datasets: [] }])}
              style={styles.addBtn}
            >
              + Add Server
            </button>
          </div>
        )}

        {activeTab === "yaml" && (
          <pre style={styles.yamlBlock}>{yamlPreview || "Click 'Materialise YAML' to generate"}</pre>
        )}
      </div>

      {isTemplate && (
        <ProductChatPanel
          open={chatOpen}
          onClose={() => setChatOpen(false)}
          ownerEmail={ownerEmail}
          projectId={null}
          request={chatRequest}
          onApply={applyTemplateSuggestion}
          getContext={templateContext}
          title="Template Authoring Assistant"
        />
      )}
    </div>
  );
}

// ── Field component ─────────────────────────────────────────────────────────

// Coerce unexpected value types (objects/arrays from non-conforming imports)
// into a readable string so the form never renders "[object Object]". For
// non-scalar values we fall back to a JSON dump in a multiline textarea.
function toFieldString(v: unknown): string {
  if (v === null || v === undefined) return "";
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  try {
    return JSON.stringify(v, null, 2);
  } catch {
    return String(v);
  }
}

function Field({ label, value, onChange, wide, multiline, options, radios, readOnly, hint }: {
  label: string; value: unknown; onChange: (v: string) => void; wide?: boolean; multiline?: boolean;
  /** Render a <select> guided by these values (∪ the current value). */
  options?: string[];
  /** Render an inline radio group (for a small closed set — quick input). */
  radios?: string[];
  /** Show the value but disable editing (e.g. an implementation-time field on a blueprint). */
  readOnly?: boolean;
  /** Small helper text under the control. */
  hint?: string;
}) {
  const str = toFieldString(value);
  const nonString = value !== undefined && value !== null && typeof value !== "string"
                    && typeof value !== "number" && typeof value !== "boolean";
  const forceMultiline = multiline || nonString || str.includes("\n");
  const opts = options && str && !options.includes(str) ? [str, ...options] : options;
  return (
    <div style={{ gridColumn: wide ? "1 / -1" : undefined }}>
      <label style={styles.fieldLabel}>
        {label}
        {nonString && (
          <span style={{ marginLeft: 6, fontSize: 10, color: "#b45309" }}>
            (imported as object — v3.1 expects a string; edit or replace)
          </span>
        )}
      </label>
      {readOnly ? (
        <input value={str} readOnly disabled style={{ ...styles.fieldInput, background: "#f1f5f9", color: "#64748b", cursor: "not-allowed" }} />
      ) : radios ? (
        <div style={{ display: "flex", gap: 14, alignItems: "center", paddingTop: 6, flexWrap: "wrap" }}>
          {radios.map((o) => (
            <label key={o} style={{ display: "inline-flex", alignItems: "center", gap: 4, fontSize: 13, cursor: "pointer" }}>
              <input type="radio" checked={str === o} onChange={() => onChange(o)} />
              {o}
            </label>
          ))}
          {str && !radios.includes(str) && (
            <span style={{ fontSize: 12, color: "#b45309" }}>({str})</span>
          )}
        </div>
      ) : opts ? (
        <select value={str} onChange={(e) => onChange(e.target.value)} style={styles.fieldInput}>
          {!str && <option value="">— select —</option>}
          {opts.map((o) => <option key={o} value={o}>{o}</option>)}
        </select>
      ) : forceMultiline ? (
        <textarea value={str} onChange={(e) => onChange(e.target.value)} rows={nonString ? 8 : 3} style={styles.fieldInput} />
      ) : (
        <input value={str} onChange={(e) => onChange(e.target.value)} style={styles.fieldInput} />
      )}
      {hint && <div style={{ fontSize: 11, color: "#94a3b8", marginTop: 3 }}>{hint}</div>}
    </div>
  );
}

// ── Styles ──────────────────────────────────────────────────────────────────

const styles: Record<string, React.CSSProperties> = {
  container: { display: "flex", flexDirection: "column", gap: 16 },
  heading: { margin: 0, fontSize: 18, fontWeight: 700, color: "#0f172a", display: "flex", alignItems: "center", gap: 10 },
  subtext: { margin: "4px 0 0", fontSize: 13, color: "#64748b" },
  statusBadge: { fontSize: 11, fontWeight: 600, padding: "2px 8px", borderRadius: 10, backgroundColor: "#fef3c7", color: "#92400e" },
  header: { display: "flex", justifyContent: "space-between", alignItems: "flex-start", flexWrap: "wrap", gap: 12 },
  headerActions: { display: "flex", gap: 8, flexWrap: "wrap" },
  primaryBtn: { padding: "8px 18px", borderRadius: 6, border: "none", backgroundColor: "#3b82f6", color: "#fff", fontWeight: 600, fontSize: 13, cursor: "pointer" },
  secondaryBtn: { padding: "8px 18px", borderRadius: 6, border: "1px solid #cbd5e1", backgroundColor: "#fff", color: "#475569", fontWeight: 600, fontSize: 13, cursor: "pointer" },
  tabBar: { display: "flex", gap: 0, borderBottom: "2px solid #e2e8f0" },
  tab: { padding: "10px 20px", border: "none", borderBottom: "2px solid transparent", marginBottom: -2, backgroundColor: "transparent", fontSize: 14, cursor: "pointer", transition: "all 0.15s" },
  tabContent: { backgroundColor: "#fff", borderRadius: "0 0 10px 10px", padding: 20, border: "1px solid #e2e8f0", borderTop: "none" },
  formGrid: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 },
  fieldLabel: { display: "block", fontSize: 12, fontWeight: 600, color: "#64748b", marginBottom: 3 },
  fieldInput: { width: "100%", padding: "7px 10px", borderRadius: 5, border: "1px solid #cbd5e1", fontSize: 13, boxSizing: "border-box" as const, fontFamily: "inherit" },
  sectionDivider: { gridColumn: "1 / -1", fontSize: 13, fontWeight: 700, color: "#334155", borderTop: "1px solid #e2e8f0", paddingTop: 10, marginTop: 4 },
  templateGrid: { display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(200px, 1fr))", gap: 12, marginTop: 16 },
  templateCard: { padding: 18, borderRadius: 10, border: "1px solid #e2e8f0", cursor: "pointer", transition: "all 0.15s", backgroundColor: "#fff" },
  templateIcon: { width: 36, height: 36, borderRadius: 8, backgroundColor: "#eff6ff", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 18, fontWeight: 700, color: "#3b82f6", marginBottom: 10 },
  templateName: { fontSize: 14, fontWeight: 600, color: "#0f172a", marginBottom: 4 },
  templateDesc: { fontSize: 12, color: "#64748b", lineHeight: 1.4 },
  schemaBlock: { marginBottom: 20, padding: 14, borderRadius: 8, border: "1px solid #e2e8f0", backgroundColor: "#f8fafc" },
  schemaHeader: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 12 },
  colTable: { width: "100%", borderCollapse: "collapse" as const, fontSize: 12 },
  colTh: { textAlign: "left" as const, padding: "6px 8px", borderBottom: "2px solid #e2e8f0", color: "#64748b", fontWeight: 600, fontSize: 11, textTransform: "uppercase" as const },
  colTd: { padding: "4px 8px", borderBottom: "1px solid #f1f5f9" },
  cellInput: { width: "100%", padding: "4px 6px", border: "1px solid #e2e8f0", borderRadius: 4, fontSize: 12, boxSizing: "border-box" as const },
  addBtn: { marginTop: 8, padding: "6px 14px", borderRadius: 5, border: "1px dashed #cbd5e1", backgroundColor: "#fff", color: "#3b82f6", fontWeight: 600, fontSize: 12, cursor: "pointer" },
  rowRemoveBtn: { padding: "2px 8px", borderRadius: 4, border: "1px solid #fecaca", backgroundColor: "#fff", color: "#dc2626", fontWeight: 700, fontSize: 12, cursor: "pointer", lineHeight: 1 },
  removeSchemaBtn: { marginTop: 8, marginLeft: 8, padding: "6px 14px", borderRadius: 5, border: "1px solid #fecaca", backgroundColor: "#fff", color: "#dc2626", fontWeight: 600, fontSize: 12, cursor: "pointer" },
  qualityRow: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 14, padding: 12, borderRadius: 8, border: "1px solid #e2e8f0", backgroundColor: "#f8fafc" },
  yamlBlock: { margin: 0, padding: 16, backgroundColor: "#0f172a", color: "#a5f3fc", borderRadius: 8, fontSize: 12, lineHeight: 1.5, overflow: "auto", maxHeight: 500, fontFamily: "'Fira Code', monospace", whiteSpace: "pre-wrap" as const },
  importPanel: { display: "flex", flexDirection: "column", gap: 6, padding: 18, borderRadius: 10, border: "1px solid #e2e8f0", backgroundColor: "#fff", marginTop: 8 },
  importError: { marginTop: 10, padding: "8px 12px", borderRadius: 6, backgroundColor: "#fef2f2", color: "#b91c1c", fontSize: 12, border: "1px solid #fecaca", whiteSpace: "pre-wrap" as const },
};
