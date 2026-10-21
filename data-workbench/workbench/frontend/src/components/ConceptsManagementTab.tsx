// Concepts management tab on the Semantic Recommender page.
//
// Renders the active :BusinessConcept tree per domain. Steward picks a
// domain, sees the nested super/value tree, can create concepts manually
// or deprecate existing ones. v1 is additive — concepts are stored as a
// new label and never touch the existing graph; an empty tree is the
// expected starting state.

import { useEffect, useState, type CSSProperties } from "react";
import api from "../api/client";
import { useConfirm } from "./dialogContext";

type Level = "entity" | "attribute" | "value";

interface ConceptNode {
  uri: string;
  name: string;
  definition: string;
  level: Level;
  value_token: string | null;
  predicate_template: string | null;
  domain: string;
  status: string;
  created_at?: string | null;
  created_by?: string | null;
  promoted_from?: string | null;
  parent_uri?: string | null;
  synonyms?: string[];
  represented_by: Array<{ column_uri: string; column_name: string }>;
  represented_by_datasets?: Array<{ dataset_uri: string; physical_name: string; relationship_kind: string }>;
  children: ConceptNode[];
}

const LEVEL_CHIP: Record<string, { bg: string; fg: string }> = {
  entity: { bg: "#dcfce7", fg: "#166534" },
  attribute: { bg: "#dbeafe", fg: "#1e40af" },
  value: { bg: "#fef3c7", fg: "#92400e" },
};

interface Relationship {
  from_uri: string;
  from_name: string;
  to_uri: string;
  to_name: string;
  kind: string;
  status: string;  // 'pending' | 'active' | 'rejected'
}

interface Props {
  // Domains the consumer can pick from. Comes from /api/domains.
  // Empty list means concepts can be created but only the default catalog
  // domains are pre-populated for selection — the new-concept form lets
  // the steward type a custom domain.
  availableDomains: string[];
}

const styles: Record<string, CSSProperties> = {
  bar: { display: "flex", alignItems: "center", gap: 12, marginBottom: 16 },
  label: { fontSize: 12, fontWeight: 600, color: "#475569" },
  select: {
    fontSize: 13, padding: "5px 8px",
    border: "1px solid #cbd5e1", borderRadius: 6,
    backgroundColor: "#fff", color: "#0f172a",
  },
  addBtn: {
    marginLeft: "auto", fontSize: 13, padding: "6px 12px",
    border: "none", borderRadius: 6,
    backgroundColor: "#3b82f6", color: "#fff", cursor: "pointer", fontWeight: 600,
  },
  empty: {
    padding: 32, borderRadius: 8, textAlign: "center",
    backgroundColor: "#f8fafc", color: "#64748b", border: "1px dashed #cbd5e1",
  },
  superCard: {
    padding: 14, borderRadius: 8, marginBottom: 12,
    backgroundColor: "#fff", border: "1px solid #e2e8f0",
  },
  superHeader: { display: "flex", alignItems: "center", gap: 10, marginBottom: 6 },
  superName: { fontSize: 15, fontWeight: 700, color: "#0f172a" },
  levelChip: {
    fontSize: 10, padding: "2px 7px", borderRadius: 4, fontWeight: 700,
    textTransform: "uppercase", letterSpacing: "0.05em",
  },
  domainChip: {
    fontSize: 10, padding: "2px 7px", borderRadius: 4, fontWeight: 700,
    backgroundColor: "#ede9fe", color: "#5b21b6",
    fontFamily: "'Fira Code', monospace",
  },
  definition: { fontSize: 13, color: "#334155", marginBottom: 8, lineHeight: 1.5 },
  bindingsRow: { fontSize: 11, color: "#64748b", marginBottom: 8 },
  uriCode: { fontFamily: "'Fira Code', monospace", fontSize: 11, color: "#475569", wordBreak: "break-all" },
  valueRow: {
    display: "grid", gridTemplateColumns: "auto 1fr auto auto", gap: 10, alignItems: "center",
    padding: "8px 10px", marginTop: 6,
    backgroundColor: "#f8fafc", borderRadius: 4, fontSize: 13,
  },
  valueName: { fontWeight: 600, color: "#0f172a" },
  valueToken: { fontFamily: "'Fira Code', monospace", fontSize: 11, color: "#475569", backgroundColor: "#fff", padding: "1px 6px", borderRadius: 4 },
  smallBtn: {
    fontSize: 11, padding: "3px 8px", border: "1px solid #cbd5e1", borderRadius: 4,
    backgroundColor: "#fff", color: "#334155", cursor: "pointer", fontWeight: 600,
  },
  destructiveBtn: {
    fontSize: 11, padding: "3px 8px", border: "1px solid #fca5a5", borderRadius: 4,
    backgroundColor: "#fff", color: "#991b1b", cursor: "pointer", fontWeight: 600,
  },
  modalOverlay: {
    position: "fixed", inset: 0, backgroundColor: "rgba(15,23,42,0.5)",
    display: "flex", alignItems: "center", justifyContent: "center", zIndex: 100,
  },
  modal: {
    backgroundColor: "#fff", borderRadius: 8, padding: 24,
    width: 520, maxWidth: "90vw", maxHeight: "90vh", overflowY: "auto",
    boxShadow: "0 10px 40px rgba(0,0,0,0.25)",
  },
  modalTitle: { fontSize: 18, fontWeight: 700, color: "#0f172a", marginBottom: 16 },
  formLabel: { fontSize: 11, fontWeight: 700, color: "#475569", textTransform: "uppercase", letterSpacing: "0.05em", marginTop: 10, marginBottom: 4, display: "block" },
  input: { width: "100%", padding: "6px 8px", border: "1px solid #cbd5e1", borderRadius: 4, fontSize: 13, boxSizing: "border-box" },
  textarea: { width: "100%", padding: "6px 8px", border: "1px solid #cbd5e1", borderRadius: 4, fontSize: 13, minHeight: 60, fontFamily: "inherit", boxSizing: "border-box" },
  modalActions: { display: "flex", gap: 8, justifyContent: "flex-end", marginTop: 16 },
  primaryBtn: {
    fontSize: 13, padding: "8px 14px", border: "none", borderRadius: 6,
    backgroundColor: "#3b82f6", color: "#fff", cursor: "pointer", fontWeight: 600,
  },
  cancelBtn: {
    fontSize: 13, padding: "8px 14px", border: "1px solid #cbd5e1", borderRadius: 6,
    backgroundColor: "#fff", color: "#334155", cursor: "pointer", fontWeight: 600,
  },
};

const DEFAULT_DOMAINS = ["customer", "products_sales", "finance", "hr", "retail banking", "common"];
const ALL_DOMAINS_TOKEN = "__all__";

export default function ConceptsManagementTab({ availableDomains }: Props) {
  const domains = availableDomains.length > 0 ? availableDomains : DEFAULT_DOMAINS;
  const confirm = useConfirm();
  const [domain, setDomain] = useState<string>(ALL_DOMAINS_TOKEN);
  const [tree, setTree] = useState<ConceptNode[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [modalParentUri, setModalParentUri] = useState<string | null>(null);
  const [relationships, setRelationships] = useState<Relationship[]>([]);
  // add-relationship form
  const [relFrom, setRelFrom] = useState("");
  const [relTo, setRelTo] = useState("");
  const [relKind, setRelKind] = useState("related_to");

  // form state
  const [fName, setFName] = useState("");
  const [fDef, setFDef] = useState("");
  const [fLevel, setFLevel] = useState<Level>("entity");
  const [fValueToken, setFValueToken] = useState("");
  const [fPredicate, setFPredicate] = useState("");
  const [fRepUris, setFRepUris] = useState("");  // newline-separated
  const [fSynonyms, setFSynonyms] = useState("");  // comma-separated
  // review filter + semantic search
  const [filter, setFilter] = useState("");
  const [semantic, setSemantic] = useState<Array<{ uri: string; name: string; level: string; score: number; entity_name: string | null }> | null>(null);
  const [semanticBusy, setSemanticBusy] = useState(false);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  // edit modal
  const [editTarget, setEditTarget] = useState<ConceptNode | null>(null);
  const [eName, setEName] = useState("");
  const [eDef, setEDef] = useState("");
  const [eSyn, setESyn] = useState("");
  const [eToken, setEToken] = useState("");
  const [ePred, setEPred] = useState("");
  // ontology diagram
  const [diagram, setDiagram] = useState<string | null>(null);
  const [diagramBusy, setDiagramBusy] = useState(false);
  const [diagramData, setDiagramData] = useState(true);  // include tables+columns

  const fetchTree = (d: string) => {
    setLoading(true);
    setError(null);
    const params = d === ALL_DOMAINS_TOKEN ? {} : { domain: d };
    api.get("/api/semantic/concepts/tree", { params })
      .then((r) => setTree((r.data?.tree as ConceptNode[]) || []))
      .catch((e) => setError(String(e?.response?.data?.detail || e?.message || e)))
      .finally(() => setLoading(false));
  };

  const fetchRelationships = (d: string) => {
    if (d === ALL_DOMAINS_TOKEN) { setRelationships([]); return; }
    api.get("/api/semantic/relationships", { params: { domain: d } })
      .then((r) => setRelationships((r.data?.relationships as Relationship[]) || []))
      .catch(() => setRelationships([]));
  };

  useEffect(() => {
    if (!domain) return;
    fetchTree(domain);
    fetchRelationships(domain);
  }, [domain]);

  const setRelStatus = async (rel: Relationship, status: "active" | "rejected") => {
    try {
      await api.post("/api/semantic/relationships", {
        from_uri: rel.from_uri, to_uri: rel.to_uri, kind: rel.kind, status,
      });
      fetchRelationships(domain);
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string };
      setError(err?.response?.data?.detail || err?.message || "Update failed");
    }
  };

  const addRelationship = async () => {
    if (!relFrom || !relTo || relFrom === relTo) {
      setError("Pick two different concepts for the relationship.");
      return;
    }
    try {
      await api.post("/api/semantic/relationships", {
        from_uri: relFrom, to_uri: relTo, kind: relKind.trim() || "related_to", status: "active",
      });
      setRelFrom(""); setRelTo(""); setRelKind("related_to");
      fetchRelationships(domain);
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string };
      setError(err?.response?.data?.detail || err?.message || "Add failed");
    }
  };

  // Child level is implied by the parent: none → entity, entity → attribute,
  // attribute → value.
  const openCreateModal = (parent: ConceptNode | null) => {
    setModalParentUri(parent ? parent.uri : null);
    setFName(""); setFDef(""); setFValueToken(""); setFPredicate(""); setFRepUris(""); setFSynonyms("");
    setFLevel(!parent ? "entity" : parent.level === "entity" ? "attribute" : "value");
    setModalOpen(true);
  };

  const fetchDiagram = async (includeData: boolean) => {
    setDiagramBusy(true);
    try {
      const r = await api.get("/api/semantic/concepts/diagram", {
        params: { domain, include_data: includeData },
      });
      setDiagram((r.data?.svg as string) || "<svg xmlns=\"http://www.w3.org/2000/svg\"/>");
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string };
      setError(err?.response?.data?.detail || err?.message || "Diagram failed");
    } finally {
      setDiagramBusy(false);
    }
  };

  const toggleDiagram = async () => {
    if (diagram !== null) { setDiagram(null); return; }
    await fetchDiagram(diagramData);
  };

  const toggleDiagramData = async () => {
    const next = !diagramData;
    setDiagramData(next);
    await fetchDiagram(next);
  };

  const runSemanticSearch = async () => {
    if (!filter.trim() || domain === ALL_DOMAINS_TOKEN) return;
    setSemanticBusy(true);
    try {
      const r = await api.post("/api/semantic/concepts/search", { domain, query: filter.trim(), k: 10 });
      setSemantic((r.data?.matches as typeof semantic) || []);
    } catch {
      setSemantic([]);
    } finally {
      setSemanticBusy(false);
    }
  };

  const toggleCollapsed = (uri: string) => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(uri)) next.delete(uri); else next.add(uri);
      return next;
    });
  };

  // Counts across the tree (entities / attributes / values).
  const counts = (() => {
    let e = 0, a = 0, v = 0;
    for (const ent of tree) {
      if (ent.level === "entity") e++;
      for (const attr of ent.children || []) {
        if (attr.level === "attribute") a++;
        v += (attr.children || []).length;
      }
    }
    return { e, a, v };
  })();

  const q = filter.trim().toLowerCase();
  const entityMatchesFilter = (ent: ConceptNode): boolean => {
    if (!q) return true;
    if (ent.name.toLowerCase().includes(q)) return true;
    return (ent.children || []).some((a) => a.name.toLowerCase().includes(q));
  };

  const openEdit = (node: ConceptNode) => {
    setEditTarget(node);
    setEName(node.name);
    setEDef(node.definition || "");
    setESyn((node.synonyms || []).join(", "));
    setEToken(node.value_token || "");
    setEPred(node.predicate_template || "");
  };

  const submitEdit = async () => {
    if (!editTarget) return;
    try {
      const body: Record<string, unknown> = {
        name: eName.trim() || undefined,
        definition: eDef.trim() || undefined,
      };
      if (editTarget.level !== "value") {
        body.synonyms = eSyn.split(",").map((s) => s.trim()).filter(Boolean);
      } else {
        if (eToken.trim()) body.value_token = eToken.trim();
        body.predicate_template = ePred; // allow clearing
      }
      await api.post(`/api/semantic/concepts/update`, { uri: editTarget.uri, ...body });
      setEditTarget(null);
      fetchTree(domain);
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string };
      setError(err?.response?.data?.detail || err?.message || "Edit failed");
    }
  };

  const submitCreate = async () => {
    if (domain === ALL_DOMAINS_TOKEN) {
      setError("Pick a specific domain in the dropdown before creating a concept.");
      return;
    }
    try {
      const body: Record<string, unknown> = {
        name: fName.trim(),
        definition: fDef.trim(),
        domain,
        level: fLevel,
      };
      if (fLevel === "value") {
        if (fValueToken.trim()) body.value_token = fValueToken.trim();
        if (fPredicate.trim()) body.predicate_template = fPredicate.trim();
      }
      if (modalParentUri) body.parent_uri = modalParentUri;
      // Bindings route by level: entity ⟶ dataset URIs, attribute ⟶ column URIs.
      const repUris = fRepUris.split("\n").map((s) => s.trim()).filter(Boolean);
      if (repUris.length > 0) {
        if (fLevel === "entity") body.represented_by_dataset_uris = repUris;
        else body.represented_by_uris = repUris;
      }
      const synonyms = fSynonyms.split(",").map((s) => s.trim()).filter(Boolean);
      if (synonyms.length > 0) body.synonyms = synonyms;
      await api.post("/api/semantic/concepts", body);
      setModalOpen(false);
      fetchTree(domain);
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string };
      setError(err?.response?.data?.detail || err?.message || "Create failed");
    }
  };

  const deprecate = async (uri: string, name: string) => {
    if (!(await confirm({
      title: `Deprecate concept "${name}"`,
      message: "It will be hidden but historical references stay intact.",
      confirmLabel: "Deprecate",
      tone: "danger",
    }))) return;
    try {
      await api.delete(`/api/semantic/concepts/${encodeURIComponent(uri)}`);
      fetchTree(domain);
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string };
      setError(err?.response?.data?.detail || err?.message || "Deprecate failed");
    }
  };

  return (
    <div>
      <div style={styles.bar}>
        <label htmlFor="concepts-domain" style={styles.label}>Domain</label>
        <select id="concepts-domain" style={styles.select} value={domain} onChange={(e) => setDomain(e.target.value)}>
          <option value={ALL_DOMAINS_TOKEN}>All domains</option>
          {domains.map((d) => <option key={d} value={d}>{d}</option>)}
        </select>
        <button
          type="button"
          style={{ ...styles.addBtn, opacity: domain === ALL_DOMAINS_TOKEN ? 0.5 : 1 }}
          onClick={() => openCreateModal(null)}
          disabled={domain === ALL_DOMAINS_TOKEN}
          title={domain === ALL_DOMAINS_TOKEN ? "Pick a specific domain to create a concept" : ""}
        >
          + Add concept
        </button>
        <button
          type="button"
          style={{ ...styles.addBtn, backgroundColor: "#0f172a", opacity: diagramBusy ? 0.5 : 1 }}
          onClick={toggleDiagram}
          disabled={diagramBusy}
          title={domain === ALL_DOMAINS_TOKEN ? "Render every domain as a lane with cross-domain links" : "Render the ontology as a layered diagram"}
        >
          {diagramBusy ? "Rendering…" : diagram !== null ? "Hide diagram" : "◫ Diagram"}
        </button>
      </div>

      {error && (
        <div style={{ padding: 10, borderRadius: 6, backgroundColor: "#fef2f2", color: "#991b1b", border: "1px solid #fecaca", marginBottom: 12, fontSize: 13 }}>
          {error}
        </div>
      )}

      {!loading && tree.length > 0 && (
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12, flexWrap: "wrap" }}>
          <span style={{ fontSize: 12, color: "#475569", fontWeight: 600 }}>
            {counts.e} {counts.e === 1 ? "entity" : "entities"} · {counts.a} attributes · {counts.v} values
          </span>
          <div style={{ marginLeft: "auto", display: "flex", gap: 6, alignItems: "center" }}>
            <input
              style={{ ...styles.select, fontSize: 13, width: 240 }}
              value={filter}
              onChange={(e) => { setFilter(e.target.value); setSemantic(null); }}
              onKeyDown={(e) => { if (e.key === "Enter") runSemanticSearch(); }}
              placeholder="Filter attributes/entities…"
            />
            <button
              type="button"
              style={{ ...styles.smallBtn, opacity: (!filter.trim() || domain === ALL_DOMAINS_TOKEN) ? 0.5 : 1 }}
              onClick={runSemanticSearch}
              disabled={!filter.trim() || domain === ALL_DOMAINS_TOKEN || semanticBusy}
              title="Semantic (embedding) search across entities + attributes"
            >
              {semanticBusy ? "Searching…" : "🔍 Semantic"}
            </button>
            {filter && (
              <button type="button" style={styles.smallBtn} onClick={() => { setFilter(""); setSemantic(null); }}>Clear</button>
            )}
          </div>
        </div>
      )}

      {semantic && (
        <div style={{ marginBottom: 12, border: "1px solid #ddd6fe", borderRadius: 8, padding: 10, backgroundColor: "#f5f3ff" }}>
          <div style={{ fontSize: 11, fontWeight: 700, color: "#6d28d9", textTransform: "uppercase", letterSpacing: 0.4, marginBottom: 6 }}>
            Semantic matches ({semantic.length})
          </div>
          {semantic.length === 0 && <div style={{ fontSize: 12, color: "#94a3b8" }}>No matches.</div>}
          {semantic.map((m) => (
            <div key={m.uri} style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13, padding: "3px 0" }}>
              <span style={{ ...styles.levelChip, backgroundColor: (LEVEL_CHIP[m.level] || LEVEL_CHIP.attribute).bg, color: (LEVEL_CHIP[m.level] || LEVEL_CHIP.attribute).fg }}>{m.level}</span>
              <span style={{ fontWeight: 600, color: "#0f172a" }}>{m.name}</span>
              {m.entity_name && m.level !== "entity" && <span style={{ fontSize: 11, color: "#64748b" }}>· {m.entity_name}</span>}
              <span style={{ marginLeft: "auto", fontSize: 11, color: "#7c3aed", fontFamily: "monospace" }}>{m.score.toFixed(3)}</span>
            </div>
          ))}
        </div>
      )}

      {diagram !== null && (
        <div style={{ marginBottom: 16, border: "1px solid #e2e8f0", borderRadius: 8, padding: 12, backgroundColor: "#fff", overflow: "auto" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 4 }}>
            <span style={{ fontSize: 12, fontWeight: 700, color: "#0f172a", textTransform: "uppercase", letterSpacing: 0.4 }}>
              Ontology — {domain === ALL_DOMAINS_TOKEN ? "all domains" : domain}
            </span>
            <label style={{ fontSize: 12, color: "#334155", display: "flex", alignItems: "center", gap: 5, cursor: "pointer", marginLeft: "auto" }}>
              <input type="checkbox" checked={diagramData} onChange={toggleDiagramData} disabled={diagramBusy} />
              Show data layer (tables + columns)
            </label>
          </div>
          <div style={{ display: "flex", gap: 14, fontSize: 11, color: "#64748b", marginBottom: 8, flexWrap: "wrap" }}>
            <span style={{ color: "#1e3a8a" }}>■ entity</span>
            <span style={{ color: "#1e40af" }}>■ attribute</span>
            <span style={{ color: "#5b21b6" }}>■ value</span>
            <span style={{ color: "#166534" }}>■ shared</span>
            {diagramData && <span style={{ color: "#a16207" }}>■ data layer</span>}
            {domain === ALL_DOMAINS_TOKEN && <span style={{ color: "#ea580c" }}>→ consumes</span>}
          </div>
          <div dangerouslySetInnerHTML={{ __html: diagram }} />
        </div>
      )}

      {domain !== ALL_DOMAINS_TOKEN && (
        <div style={{ marginBottom: 16, border: "1px solid #e2e8f0", borderRadius: 8, padding: 12, backgroundColor: "#faf5ff" }}>
          <div style={{ fontSize: 12, fontWeight: 700, color: "#6d28d9", textTransform: "uppercase", letterSpacing: 0.4, marginBottom: 8 }}>
            Concept relationships ({relationships.length})
          </div>
          <div style={{ fontSize: 11, color: "#64748b", marginBottom: 10 }}>
            Approved (active) relationships pull neighbour concepts into Concept-Guided chat answers.
            Seeded automatically when you accept recommendations, or add them below.
          </div>

          {relationships.length === 0 && (
            <div style={{ fontSize: 12, color: "#94a3b8", fontStyle: "italic", marginBottom: 10 }}>
              No relationships yet for {domain}. Add one below.
            </div>
          )}

          {relationships.map((rel, i) => (
            <div key={i} style={{
              display: "flex", alignItems: "center", gap: 8, padding: "6px 0",
              borderTop: i === 0 ? "none" : "1px solid #ede9fe", fontSize: 13,
            }}>
              <span style={{ fontWeight: 600, color: "#0f172a" }}>{rel.from_name}</span>
              <span style={{ fontSize: 11, padding: "1px 7px", borderRadius: 4, backgroundColor: "#ede9fe", color: "#5b21b6", fontWeight: 600 }}>
                {rel.kind}
              </span>
              <span style={{ color: "#64748b" }}>→</span>
              <span style={{ fontWeight: 600, color: "#0f172a" }}>{rel.to_name}</span>
              <span style={{
                fontSize: 10, padding: "1px 6px", borderRadius: 4, fontWeight: 700, textTransform: "uppercase",
                backgroundColor: rel.status === "active" ? "#dcfce7" : "#fef3c7",
                color: rel.status === "active" ? "#166534" : "#92400e",
              }}>{rel.status}</span>
              <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
                {rel.status !== "active" && (
                  <button type="button" style={styles.smallBtn} onClick={() => setRelStatus(rel, "active")}>Approve</button>
                )}
                <button type="button" style={styles.destructiveBtn} onClick={() => setRelStatus(rel, "rejected")}>Reject</button>
              </div>
            </div>
          ))}

          {/* Manual add — options are the active super-concepts in the tree */}
          <div style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 10, paddingTop: 10, borderTop: "1px solid #ede9fe", flexWrap: "wrap" }}>
            <select style={{ ...styles.select, fontSize: 12 }} value={relFrom} onChange={(e) => setRelFrom(e.target.value)}>
              <option value="">— from concept —</option>
              {tree.filter((c) => c.level === "entity").map((c) => <option key={c.uri} value={c.uri}>{c.name}</option>)}
            </select>
            <input
              style={{ ...styles.select, fontSize: 12, width: 120 }}
              value={relKind}
              onChange={(e) => setRelKind(e.target.value)}
              placeholder="kind"
              title="Relationship kind, e.g. related_to, belongs_to, derived_from"
            />
            <select style={{ ...styles.select, fontSize: 12 }} value={relTo} onChange={(e) => setRelTo(e.target.value)}>
              <option value="">— to concept —</option>
              {tree.filter((c) => c.level === "entity").map((c) => <option key={c.uri} value={c.uri}>{c.name}</option>)}
            </select>
            <button
              type="button"
              style={{ ...styles.smallBtn, opacity: (!relFrom || !relTo || relFrom === relTo) ? 0.5 : 1 }}
              onClick={addRelationship}
              disabled={!relFrom || !relTo || relFrom === relTo}
            >
              + Add relationship
            </button>
          </div>
        </div>
      )}

      {loading && <div style={styles.empty}>Loading…</div>}

      {!loading && tree.length === 0 && (
        <div style={styles.empty}>
          <div style={{ fontSize: 14, marginBottom: 8 }}>
            No business concepts yet
            {domain === ALL_DOMAINS_TOKEN ? "" : <> for <strong>{domain}</strong></>}.
          </div>
          <div style={{ fontSize: 12 }}>
            {domain === ALL_DOMAINS_TOKEN
              ? "Pick a specific domain to add a concept, or accept a recommendation from the Recommendations tab."
              : <>Click <strong>+ Add concept</strong> to create one manually, or accept a recommendation from the Recommendations tab.</>}
          </div>
        </div>
      )}

      {!loading && tree.filter((c) => c.level !== "value" && entityMatchesFilter(c)).map((entity) => {
        const chip = LEVEL_CHIP[entity.level] || LEVEL_CHIP.attribute;
        const tables = entity.represented_by_datasets || [];
        const isCollapsed = collapsed.has(entity.uri);
        // When filtering by text, show only matching attributes (unless the
        // entity name itself matched, in which case show all).
        const entityNameHit = !q || entity.name.toLowerCase().includes(q);
        const attrs = (entity.children || []).filter(
          (a) => entityNameHit || a.name.toLowerCase().includes(q),
        );
        return (
          <div key={entity.uri} style={styles.superCard}>
            <div style={styles.superHeader}>
              <button
                type="button"
                onClick={() => toggleCollapsed(entity.uri)}
                style={{ border: "none", background: "none", cursor: "pointer", fontSize: 12, color: "#64748b", padding: 0, width: 16 }}
                title={isCollapsed ? "Expand" : "Collapse"}
              >{isCollapsed ? "▸" : "▾"}</button>
              <span style={styles.superName}>{entity.name}</span>
              <span style={{ ...styles.levelChip, backgroundColor: chip.bg, color: chip.fg }}>{entity.level}</span>
              <span style={{ fontSize: 11, color: "#64748b" }}>{(entity.children || []).length} attributes</span>
              {entity.domain && <span style={styles.domainChip} title="Domain">{entity.domain}</span>}
              {tables.map((t) => (
                <span key={t.dataset_uri} title={`table · ${t.relationship_kind}`} style={{
                  fontSize: 11, padding: "1px 7px", borderRadius: 4, fontWeight: 600,
                  backgroundColor: "#ecfccb", color: "#3f6212",
                }}>⊞ {t.physical_name}</span>
              ))}
              <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
                <button style={styles.smallBtn} onClick={() => openEdit(entity)}>Edit</button>
                <button style={styles.smallBtn} onClick={() => openCreateModal(entity)}>
                  {entity.level === "entity" ? "+ Add attribute" : "+ Add value"}
                </button>
                <button style={styles.destructiveBtn} onClick={() => deprecate(entity.uri, entity.name)}>Deprecate</button>
              </div>
            </div>
            <div style={styles.definition}>{entity.definition}</div>
            {entity.synonyms && entity.synonyms.length > 0 && (
              <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginBottom: 8, alignItems: "center" }}>
                <span style={{ fontSize: 11, fontWeight: 600, color: "#5b21b6", marginRight: 4 }}>synonyms:</span>
                {entity.synonyms.map((s) => (
                  <span key={s} style={{ fontSize: 11, padding: "1px 7px", borderRadius: 4, fontWeight: 600, backgroundColor: "#ede9fe", color: "#5b21b6" }}>{s}</span>
                ))}
              </div>
            )}

            {/* Attributes — first-class rows */}
            {!isCollapsed && attrs.map((attr) => (
              <div key={attr.uri} style={{ marginTop: 6, padding: "6px 8px", borderRadius: 6, backgroundColor: "#f8fafc", border: "1px solid #eef2f7" }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <span title="embedded & semantically searchable" style={{ color: "#7c3aed", fontSize: 10 }}>●</span>
                  <span style={{ fontWeight: 600, color: "#0f172a", fontSize: 13 }}>{attr.name}</span>
                  {attr.children.length > 0 && (
                    <span style={{ fontSize: 10, padding: "0 6px", borderRadius: 8, backgroundColor: "#fef3c7", color: "#92400e", fontWeight: 700 }}>
                      {attr.children.length} values
                    </span>
                  )}
                  {attr.represented_by.length > 0 && (
                    <span style={{ fontSize: 11, color: "#64748b", fontFamily: "monospace" }} title={attr.represented_by.map((b) => b.column_uri).join("\n")}>
                      {attr.represented_by.map((b) => b.column_name).join(", ")}
                    </span>
                  )}
                  <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
                    <button style={styles.smallBtn} onClick={() => openEdit(attr)}>Edit</button>
                    <button style={styles.smallBtn} onClick={() => openCreateModal(attr)}>+ Add value</button>
                    <button style={styles.destructiveBtn} onClick={() => deprecate(attr.uri, attr.name)}>Deprecate</button>
                  </div>
                </div>
                {attr.definition && attr.definition !== `${attr.name} — attribute of ${entity.name}.` && (
                  <div style={{ fontSize: 12, color: "#475569", marginTop: 2 }}>{attr.definition}</div>
                )}
                {/* Values */}
                {attr.children.map((v) => (
                  <div key={v.uri} style={styles.valueRow}>
                    <span style={styles.valueName}>{v.name}</span>
                    <span style={styles.valueToken} title="value_token">
                      {v.value_token || v.predicate_template || "(no predicate)"}
                    </span>
                    <button style={styles.smallBtn} onClick={() => openEdit(v)}>Edit</button>
                    <button style={styles.destructiveBtn} onClick={() => deprecate(v.uri, v.name)}>Deprecate</button>
                  </div>
                ))}
              </div>
            ))}
          </div>
        );
      })}

      {modalOpen && (
        <div style={styles.modalOverlay} onClick={() => setModalOpen(false)}>
          <div style={styles.modal} onClick={(e) => e.stopPropagation()}>
            <div style={styles.modalTitle}>
              {fLevel === "entity" ? "Add entity" : fLevel === "attribute" ? "Add attribute" : "Add value"}
            </div>
            <label style={styles.formLabel}>Name</label>
            <input style={styles.input} value={fName} onChange={(e) => setFName(e.target.value)} placeholder="e.g. Customer, Lifetime Value, or Cancelled Orders" />
            <label style={styles.formLabel}>Definition</label>
            <textarea style={styles.textarea} value={fDef} onChange={(e) => setFDef(e.target.value)} placeholder="1-sentence definition" />
            {!modalParentUri && (
              <>
                <label style={styles.formLabel}>Level</label>
                <select style={styles.select} value={fLevel} onChange={(e) => setFLevel(e.target.value as Level)}>
                  <option value="entity">Entity (business object — binds to a table)</option>
                  <option value="attribute">Attribute (property — binds to a column)</option>
                  <option value="value">Value (specific instance)</option>
                </select>
              </>
            )}
            {fLevel === "value" && (
              <>
                <label style={styles.formLabel}>Value token (matched against the bound column)</label>
                <input style={styles.input} value={fValueToken} onChange={(e) => setFValueToken(e.target.value)} placeholder="e.g. cancelled" />
                <label style={styles.formLabel}>Predicate template (optional override)</label>
                <input style={styles.input} value={fPredicate} onChange={(e) => setFPredicate(e.target.value)} placeholder="e.g. status IN ('cancel_pending','cancelled')" />
              </>
            )}
            {fLevel !== "value" && (
              <>
                <label style={styles.formLabel}>Synonyms (comma-separated)</label>
                <input
                  style={styles.input}
                  value={fSynonyms}
                  onChange={(e) => setFSynonyms(e.target.value)}
                  placeholder="e.g. loyalty tier, membership level"
                />
                <label style={styles.formLabel}>
                  {fLevel === "entity"
                    ? "Represented by — :DProdOutputDataset URIs (one per line)"
                    : "Represented by — :DProdColumn URIs (one per line)"}
                </label>
                <textarea style={styles.textarea} value={fRepUris} onChange={(e) => setFRepUris(e.target.value)} placeholder={fLevel === "entity" ? "dprod:ds:..." : "dprod:col:..."} />
              </>
            )}
            <div style={styles.modalActions}>
              <button style={styles.cancelBtn} onClick={() => setModalOpen(false)}>Cancel</button>
              <button style={styles.primaryBtn} onClick={submitCreate} disabled={!fName.trim() || !fDef.trim()}>
                Create
              </button>
            </div>
          </div>
        </div>
      )}

      {editTarget && (
        <div style={styles.modalOverlay} onClick={() => setEditTarget(null)}>
          <div style={styles.modal} onClick={(e) => e.stopPropagation()}>
            <div style={styles.modalTitle}>Edit {editTarget.level}</div>
            <label style={styles.formLabel}>Name</label>
            <input style={styles.input} value={eName} onChange={(e) => setEName(e.target.value)} />
            <label style={styles.formLabel}>Definition</label>
            <textarea style={styles.textarea} value={eDef} onChange={(e) => setEDef(e.target.value)} />
            {editTarget.level !== "value" ? (
              <>
                <label style={styles.formLabel}>Synonyms (comma-separated)</label>
                <input style={styles.input} value={eSyn} onChange={(e) => setESyn(e.target.value)} />
              </>
            ) : (
              <>
                <label style={styles.formLabel}>Value token (the literal used in the predicate)</label>
                <input style={styles.input} value={eToken} onChange={(e) => setEToken(e.target.value)} />
                <label style={styles.formLabel}>Predicate template (optional override)</label>
                <input style={styles.input} value={ePred} onChange={(e) => setEPred(e.target.value)} />
              </>
            )}
            <div style={styles.modalActions}>
              <button style={styles.cancelBtn} onClick={() => setEditTarget(null)}>Cancel</button>
              <button style={styles.primaryBtn} onClick={submitEdit} disabled={!eName.trim()}>Save</button>
            </div>
          </div>
        </div>
      )}

    </div>
  );
}
