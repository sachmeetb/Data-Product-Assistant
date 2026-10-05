import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import api from "../api/client";
import { downloadBlobZip } from "../lib/download";
import { useRole } from "../RoleContext";
import { useCurrentUserEmail } from "../AuthContext";
import {
  categorizeRuleType,
  RULE_CATEGORIES,
  CATEGORY_COLORS,
  type RuleCategory,
} from "../lib/ruleCategory";
import MappingGraphView, { type GraphPayload } from "../components/MappingGraphView";
import OsiAnalysisPanel from "../components/OsiAnalysisPanel";
import OsiBadge, { type OsiBand } from "../components/OsiBadge";
import QuestionsPanel, { type QaEvaluationPayload } from "../components/QuestionsPanel";
import { usePrompt, useNotify } from "../components/dialogContext";
import ProductKindChip from "../components/ProductKindChip";
import RelationshipKindChip from "../components/RelationshipKindChip";
import RevisionTimeline from "../components/RevisionTimeline";
import SensitivityChip from "../components/SensitivityChip";
import ServingWarningsCallout from "../components/ServingWarningsCallout";
import { servingModeLabel, servingModeColor } from "../lib/servingMode";
import PreviewTab from "../components/PreviewTab";
import DeployChecklistModal from "../components/DeployChecklistModal";
import MarketplaceChatPanel from "../components/MarketplaceChatPanel";
import DeploymentReflectionPanel from "../components/DeploymentReflectionPanel";
import DatasetsCatalog from "../components/DatasetsCatalog";
import ProductLineageView from "../components/ProductLineageView";
import FlowSankeyCatalogTab from "../components/flow-sankey/FlowSankeyCatalogTab";


interface Product {
  uri: string;
  name: string;
  status: string;
  lifecycle_state: string | null;
  has_inflight_edit: boolean | null;
  lifecycle_version: number | null;
  contract_id: string | null;
  project_id: number | null;
  published_at: string | null;
  published_by: string | null;
  created_at: string | null;
  description: string | null;
  purpose: string | null;
  owner_name: string | null;
  owner_role: string | null;
  column_count: number;
  // 'source' | 'consumer' — drives the ProductKindChip badge.
  product_kind: string | null;
  // Free-form product tags; drive the tag filter + group-by controls.
  tags: string[];
  osi_band: OsiBand;
  osi_completeness: number | null;
  osi_conformance_pass: boolean | null;
  osi_evaluated_at: string | null;
  // Rubric the eval was scored against (e.g. 'osi' / 'ai_ready') plus the
  // pre-resolved chip label from the backend's rubric catalog. Drives the
  // OsiBadge chip label.
  scoring_rubric: string | null;
  rubric_short_label: string | null;
}

interface ProductDetail {
  uri: string;
  name: string;
  status: string;
  lifecycle_state: string | null;
  has_inflight_edit: boolean | null;
  lifecycle_version: number | null;
  contract_id: string | null;
  project_id: number | null;
  title: string | null;
  description: string | null;
  purpose: string | null;
  limitations: string | null;
  domain: string | null;
  data_product: string | null;
  published_at: string | null;
  published_by: string | null;
  owner_name: string | null;
  owner_role: string | null;
  owner_email: string | null;
  owners: Array<{ username: string | null; name: string | null; role: string | null; email: string | null }>;
  stewards: Array<{ name: string | null; email: string | null; role: string | null; username: string | null }>;
  team: Array<{ username: string | null; role: string | null; name: string | null; email: string | null }>;
  roles: Array<{ role: string; description: string | null; access: string | null; datasets: string[] | null }>;
  servers: Array<{
    name: string | null;
    environment: string | null;
    type: string | null;
    account: string | null;
    database: string | null;
    schema: string | null;
    datasets: unknown[];
  }>;
  tags: string[];
  support: {
    contacts?: Array<{ type?: string; value?: string }>;
    documentation?: Array<{ type?: string; url?: string }>;
    escalationPolicy?: string;
  } | null;
  custom_properties: Record<string, unknown> | null;
  // 'source' | 'consumer' — surfaced as a chip in the detail header.
  product_kind: string | null;
  terms_usage: string | null;
  terms_limitations: string | null;
  terms_billing: string | null;
  terms_notice_period: string | null;
  slas: Array<{ property: string; value: string; unit: string }>;
  quality_rules: Array<{
    rule: string;
    name: string | null;
    description: string | null;
    severity: string;
    dimension: string | null;
    businessImpact: string | null;
    /** marketplace.py emits 'contract' (top-level quality[]) or
     *  'domain' / 'user' / 'spec' (PropertyShape-anchored). */
    source: string | null;
    /** Empty when the contract rule has no column hint and no per-column
     *  anchor — those rules group into the "(contract-wide)" pseudo-table. */
    column_name: string | null;
    /** Dataset physical name for column-anchored rules; empty for
     *  contract-wide rules without a dataset hint. */
    dataset_name: string | null;
  }>;
  datasets: Array<{
    uri?: string | null;
    name: string | null;
    physicalName: string | null;
    description: string | null;
    relationshipKind: string | null;
    columns: Array<{ name: string; logicalName: string | null; logicalType: string | null; physicalType: string; description: string | null; primaryKey: boolean; sensitivity?: string | null }>;
  }>;
  serving: Array<{ servingMode: string; viewName: string | null; viewSchema: string | null; targetPlatform: string | null; ddl: string | null; summaryJson: string | null;
    dbtMaterialization?: string | null; targetSchema?: string | null; buildStatus?: string | null; builtAt?: string | null; buildError?: string | null; modelsJson?: string | null }>;
  // Cross-product relationships. consumes is populated for consumer-aligned
  // products (the source products this product :CONSUMES); consumed_by is
  // populated for source-aligned products (consumer contracts that consume
  // this one). Both are click-throughs into the related product's detail.
  consumes: Array<{ uri: string; name: string; product_kind: string }>;
  consumed_by: Array<{ uri: string; name: string; product_kind: string; lifecycle_state: string }>;
  osi_band: OsiBand;
  osi_completeness: number | null;
  osi_conformance_pass: boolean | null;
  osi_evaluated_at: string | null;
  /** Selected readiness rubric + pre-resolved chip label. Detail-page
   *  OsiBadge reads short_label from here. */
  scoring_rubric: string | null;
  rubric_short_label: string | null;
  /** Latest :QAEvaluation sidecar for this contract; null until first run. */
  qa_evaluation: (QaEvaluationPayload & { uri: string; mode: string }) | null;
  /** Phase 1 versioning: list of :ContractVersion sidecars (newest first) */
  available_versions?: Array<{
    version: number;
    lifecycle_state: string | null;
    change_kind: string | null;
    revision_notes: string | null;
    published_at: string | null;
    occurred_at: string | null;
    is_current: boolean;
  }>;
  /** Set when the detail was rendered with an explicit ?version=N pin. */
  viewing_version?: number | null;
}

interface LineageSource {
  source_system: string | null;
  source_schema: string | null;
  source_table: string;
  mapped_columns?: number;
  source_columns?: number;
  /** True for reference tables a lookup-kind mapping reads from (often a
   *  different CONSUMES'd product). Shown with a "lookup" tag instead of an
   *  X/Y mapped ratio. */
  is_lookup?: boolean;
  /** Distinct lookup columns referenced (when is_lookup). */
  lookup_columns?: number;
}

interface LineageData {
  sources: LineageSource[];
  stats: { total_columns: number; total_mappings: number; total_quality_rules: number };
}

function ServingGitLinks({ projectId }: { projectId: number }) {
  // Marketplace is view-only for git: pushing is an engineer action (the
  // Engineering serving card + Pipeline own the Push button), so here we surface
  // only the "View in Git" link when the product's repo exists.
  const [repoUrl, setRepoUrl] = useState<string | null>(null);
  useEffect(() => {
    api.get(`/api/projects/${projectId}/serving/git-status`)
      .then((r) => setRepoUrl(r.data.repo_url || null))
      .catch(() => {});
  }, [projectId]);
  if (!repoUrl) return null;
  return (
    <a href={repoUrl} target="_blank" rel="noreferrer"
      style={{ fontSize: 12, fontWeight: 600, color: "#7c3aed", textDecoration: "none" }}>
      View in Git →
    </a>
  );
}

export default function MarketplacePage() {
  const navigate = useNavigate();
  const role = useRole();
  const prompt = usePrompt();
  const { showError } = useNotify();
  const CURRENT_USER_EMAIL = useCurrentUserEmail();
  const { uri: uriParam } = useParams<{ uri?: string }>();
  const [products, setProducts] = useState<Product[]>([]);
  const [loading, setLoading] = useState(true);
  // 'all' | 'source' | 'consumer' — filters the marketplace product list.
  // Backend supports it via /api/marketplace?product_kind=source|consumer; we
  // also filter client-side so toggling between chips is instantaneous (no
  // re-fetch on every click).
  const [kindFilter, setKindFilter] = useState<"all" | "source" | "aggregate" | "consumer">("all");
  // Free-form tag filter (multi-select, OR semantics) + a group-by-tag toggle
  // that buckets the listing under tag headers. Both client-side over the
  // already-loaded set so toggling is instant; the backend `?tag=` param is for
  // MCP / deep-links.
  const [selectedTags, setSelectedTags] = useState<string[]>([]);
  const [groupByTag, setGroupByTag] = useState(false);
  // Top-level marketplace sub-tab: published data products, discovered datasets,
  // or the product-to-product lineage graph.
  const [catalogView, setCatalogView] = useState<"products" | "datasets" | "lineage" | "flow">("products");
  // Tracks an in-flight OSI re-evaluation so the button can disable + show
  // pending state. The /osi/evaluate endpoint runs translate→validate→score
  // synchronously which can take a few seconds.
  const [scoringOsi, setScoringOsi] = useState(false);
  // Inline tag editing on the product detail (PO-facing "add tags later").
  const [editingTags, setEditingTags] = useState(false);
  const [tagsDraft, setTagsDraft] = useState<string[]>([]);
  const [tagDraftInput, setTagDraftInput] = useState("");
  const [savingTags, setSavingTags] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  // Phase 1 versioning: when null, the detail endpoint auto-pins to latest
  // deployed; when set, ?version=N is passed so a historical view renders.
  const [selectedVersion, setSelectedVersion] = useState<number | null>(null);
  const [detail, setDetail] = useState<ProductDetail | null>(null);
  const [lineage, setLineage] = useState<LineageData | null>(null);
  const [mappingGraph, setMappingGraph] = useState<GraphPayload | null>(null);
  const [detailTab, setDetailTab] = useState<"overview" | "schema" | "quality" | "osi" | "qa" | "lineage" | "serving" | "preview" | "reflection" | "terms" | "team" | "roles" | "servers">("overview");
  const [ddlExpanded, setDdlExpanded] = useState(false);
  const [expandedModelSql, setExpandedModelSql] = useState<Record<string, boolean>>({});
  const [deploying, setDeploying] = useState(false);
  const [showDeployChecklist, setShowDeployChecklist] = useState(false);
  const [generatingReport, setGeneratingReport] = useState(false);
  const [downloadingYaml, setDownloadingYaml] = useState(false);
  const [downloadingDbt, setDownloadingDbt] = useState(false);
  const [downloadingOkf, setDownloadingOkf] = useState(false);
  const [exportingMarketplaceOkf, setExportingMarketplaceOkf] = useState(false);
  const [copyToast, setCopyToast] = useState("");
  // Phase 3b: marketplace free-form chat drawer.
  const [chatOpen, setChatOpen] = useState(false);

  const refreshList = () =>
    api.get("/api/marketplace").then((r) => setProducts(r.data.products)).catch(() => {});

  useEffect(() => {
    refreshList().finally(() => setLoading(false));
  }, []);

  const selectProduct = async (uri: string, version: number | null = null) => {
    setSelected(uri);
    setSelectedVersion(version);
    setDetailTab("overview");
    try {
      const detailParams: Record<string, string | number> = { uri };
      if (version !== null) detailParams.version = version;
      const [detailRes, lineageRes, graphRes] = await Promise.all([
        api.get("/api/marketplace/detail", { params: detailParams }),
        api.get("/api/marketplace/lineage", { params: { uri } }),
        api.get("/api/marketplace/mapping-graph", { params: { uri } }).catch(() => null),
      ]);
      setDetail(detailRes.data);
      setLineage(lineageRes.data);
      setMappingGraph(graphRes ? graphRes.data : null);
    } catch {
      setDetail(null);
      setLineage(null);
      setMappingGraph(null);
    }
  };

  // Eligibility for the PO Deploy gesture: role must be PO (the marketplace
  // is shared between shells), the contract must be in 'approved' (engineer
  // marked complete) OR have an in-flight edit waiting on a PO Deploy, and
  // the current user must be one of the product's owners.
  const canDeploy = (d: ProductDetail | null): boolean => {
    if (!d || role !== "Data Product Owner" || !d.project_id) return false;
    const isOwner = (d.owners || []).some(
      (o) => (o.email || "").toLowerCase() === CURRENT_USER_EMAIL.toLowerCase()
    );
    if (!isOwner) return false;
    if (d.lifecycle_state === "approved") return true;
    if (d.lifecycle_state === "published" && d.has_inflight_edit) return true;
    return false;
  };

  // PO-only "Score OSI now" gesture. Hits the existing evaluate endpoint
  // with trigger='manual' and refreshes the detail panel so the chip /
  // stat-card reflect the new band+completeness. Eligibility mirrors the
  // Deploy gesture: only owners on a project that exists.
  const canScoreOsi = (d: ProductDetail | null): boolean => {
    if (!d || role !== "Data Product Owner" || !d.project_id) return false;
    const isOwner = (d.owners || []).some(
      (o) => (o.email || "").toLowerCase() === CURRENT_USER_EMAIL.toLowerCase()
    );
    return isOwner;
  };

  // PO-only inline tag editing on the product detail — the discoverable
  // "add tags after the fact" surface. Same owner-gate as Score OSI.
  const canEditTags = (d: ProductDetail | null): boolean => {
    if (!d || role !== "Data Product Owner" || !d.project_id) return false;
    return (d.owners || []).some(
      (o) => (o.email || "").toLowerCase() === CURRENT_USER_EMAIL.toLowerCase()
    );
  };

  const beginEditTags = () => {
    setTagsDraft(detail?.tags ? [...detail.tags] : []);
    setTagDraftInput("");
    setEditingTags(true);
  };
  const addTagDraft = (raw: string) => {
    const label = raw.trim();
    if (!label) return;
    setTagsDraft((cur) => (cur.some((t) => t.toLowerCase() === label.toLowerCase()) ? cur : [...cur, label]));
    setTagDraftInput("");
  };
  const handleSaveTags = async () => {
    if (!detail || !detail.project_id || savingTags) return;
    // Fold any half-typed token in the input into the set before saving.
    const pending = tagDraftInput.trim();
    const finalTags = pending && !tagsDraft.some((t) => t.toLowerCase() === pending.toLowerCase())
      ? [...tagsDraft, pending]
      : tagsDraft;
    setSavingTags(true);
    try {
      const res = await api.put(`/api/projects/${detail.project_id}/odcs/tags`, { tags: finalTags });
      const saved: string[] = res.data?.tags ?? finalTags;
      setDetail((d) => (d ? { ...d, tags: saved } : d));
      setProducts((ps) => ps.map((p) => (p.uri === detail.uri ? { ...p, tags: saved } : p)));
      setEditingTags(false);
    } catch (e) {
      showError(e, { title: "Saving tags failed" });
    }
    setSavingTags(false);
  };

  const handleScoreOsi = async () => {
    if (!detail || !detail.project_id || scoringOsi) return;
    setScoringOsi(true);
    try {
      await api.post(`/api/projects/${detail.project_id}/osi/evaluate`, {
        trigger: "manual",
        skip_advisor: true,
      });
      await selectProduct(detail.uri);
    } catch (e) {
      showError(e, { title: "OSI scoring failed" });
    }
    setScoringOsi(false);
  };

  // The new version's ProductRequest must be in status='complete' before
  // /odcs/publish flips lifecycleState — we don't enforce that client-side,
  // the backend rejects with a 400 if mistimed. Errors get surfaced via alert
  // for now (matches the Pipeline's STAGE_ACTIONS error pattern).
  const handleDeploy = async () => {
    if (!detail || !detail.project_id || deploying) return;
    setDeploying(true);
    try {
      await api.post(`/api/projects/${detail.project_id}/odcs/publish`);
      setShowDeployChecklist(false);
      await Promise.all([refreshList(), selectProduct(detail.uri)]);
    } catch (e) {
      console.error(e);
      showError(e, { title: "Deploy failed" });
    }
    setDeploying(false);
  };

  // Derived products (consumer + aggregate — both compose upstream products)
  // get the pre-deploy checklist reminder; source products keep the one-click
  // deploy (their servers/SLA come from the source flow).
  const onDeployClick = () => {
    if (detail?.product_kind && detail.product_kind !== "source") {
      setShowDeployChecklist(true);
    } else {
      handleDeploy();
    }
  };

  // Download a Markdown report for the selected product. The endpoint
  // returns text/markdown with Content-Disposition; we build a Blob and
  // trigger a download from the client so the file lands in the user's
  // Downloads folder regardless of browser settings.
  const handleGenerateReport = async (d: ProductDetail) => {
    if (!d.contract_id || generatingReport) return;
    setGeneratingReport(true);
    try {
      // axios with responseType:'text' so the SSE Content-Type doesn't trip
      // the default JSON parser. The endpoint sets text/markdown anyway.
      const resp = await api.get(`/api/marketplace/${encodeURIComponent(d.contract_id)}/report`, {
        responseType: "text",
        transformResponse: (x) => x,  // disable JSON auto-parse
      });
      const blob = new Blob([resp.data as string], { type: "text/markdown;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      const ts = new Date().toISOString().slice(0, 19).replace(/[-:T]/g, "");
      const safeName = (d.name || d.contract_id).replace(/[^A-Za-z0-9_.-]+/g, "-");
      a.download = `${safeName}-${ts}.md`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      console.error(e);
      showError(e, { title: "Report generation failed" });
    }
    setGeneratingReport(false);
  };

  // Download the canonical ODCS v3.1 YAML for the selected product. Mirror
  // of handleGenerateReport: hit the stable /api/marketplace/<id>/odcs.yaml
  // endpoint and trigger a browser save via Blob + a.download so the file
  // lands in Downloads with a deterministic filename. The same endpoint
  // serves curl / CI / catalog-import callers — see handleCopyOdcsUrl.
  const handleDownloadOdcsYaml = async (d: ProductDetail) => {
    if (!d.contract_id || downloadingYaml) return;
    setDownloadingYaml(true);
    try {
      const resp = await api.get(`/api/marketplace/${encodeURIComponent(d.contract_id)}/odcs.yaml`, {
        responseType: "text",
        transformResponse: (x) => x,
      });
      const blob = new Blob([resp.data as string], { type: "application/x-yaml;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      const ts = new Date().toISOString().slice(0, 19).replace(/[-:T]/g, "");
      const safeName = (d.name || d.contract_id).replace(/[^A-Za-z0-9_.-]+/g, "-");
      a.download = `${safeName}-${ts}.odcs.yaml`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      console.error(e);
      showError(e, { title: "ODCS YAML download failed" });
    }
    setDownloadingYaml(false);
  };

  // Download the generated dbt project (zip) so an engineer can run/maintain it.
  const handleDownloadDbtProject = async (d: ProductDetail) => {
    if (!d.project_id || downloadingDbt) return;
    setDownloadingDbt(true);
    try {
      const resp = await api.get(`/api/projects/${d.project_id}/serving/dbt-project?format=zip`, {
        responseType: "blob",
      });
      const url = URL.createObjectURL(resp.data as Blob);
      const a = document.createElement("a");
      a.href = url;
      const safeName = (d.name || String(d.project_id)).replace(/[^A-Za-z0-9_.-]+/g, "-");
      a.download = `${safeName}-dbt.zip`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      console.error(e);
      showError(e, { title: "dbt project download failed" });
    }
    setDownloadingDbt(false);
  };

  // Download a zip from a Content-Disposition endpoint, preserving the
  // server-supplied filename. Shared helper in lib/download.ts.
  const downloadZip = downloadBlobZip;

  // Export this single product as an Open Knowledge Format bundle (zip of
  // cross-linked markdown + frontmatter) for an external AI agent.
  const handleExportOkf = async (d: ProductDetail) => {
    if (!d.contract_id || downloadingOkf) return;
    setDownloadingOkf(true);
    try {
      await downloadZip(
        `/api/marketplace/products/${encodeURIComponent(d.contract_id)}/okf-bundle?format=zip`,
        `okf-${d.contract_id}.zip`,
      );
    } catch (e) {
      console.error(e);
      showError(e, { title: "OKF export failed" });
    }
    setDownloadingOkf(false);
  };

  // Export the whole marketplace as one cross-linked OKF bundle.
  const handleExportMarketplaceOkf = async () => {
    if (exportingMarketplaceOkf) return;
    setExportingMarketplaceOkf(true);
    try {
      await downloadZip("/api/marketplace/okf-bundle?format=zip", "okf-marketplace.zip");
    } catch (e) {
      console.error(e);
      showError(e, { title: "OKF export failed" });
    }
    setExportingMarketplaceOkf(false);
  };

  // Copy the stable ODCS YAML URL to clipboard so consumers can paste it
  // into curl / CI configs / catalog imports without going through the UI.
  const handleCopyOdcsUrl = async (d: ProductDetail) => {
    if (!d.contract_id) return;
    const url = `${window.location.origin}/api/marketplace/${encodeURIComponent(d.contract_id)}/odcs.yaml`;
    try {
      await navigator.clipboard.writeText(url);
      setCopyToast("ODCS URL copied");
      setTimeout(() => setCopyToast(""), 2000);
    } catch {
      // Fallback when clipboard API is unavailable (insecure context, etc.)
      await prompt({ title: "Copy this URL", defaultValue: url, confirmLabel: "Done" });
    }
  };

  // Deep-link support: /product/marketplace/:uri (and /engineer/marketplace/:uri)
  // opens the detail panel for the given product. My Products dashboard relies
  // on this to navigate into product detail.
  useEffect(() => {
    if (!uriParam) return;
    const decoded = decodeURIComponent(uriParam);
    if (decoded !== selected) {
      selectProduct(decoded);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [uriParam]);

  if (loading) return <div style={{ color: "#94a3b8", padding: 24 }}>Loading marketplace...</div>;

  // ── Tag filter + group-by (client-side over the loaded set) ──────────────
  // Union of every product's tags, deduped case-insensitively (first-seen case
  // wins), alphabetised — the chips the PO can toggle.
  const allTags: string[] = (() => {
    const seen = new Map<string, string>();
    for (const p of products) for (const t of (p.tags || [])) {
      const key = t.trim().toLowerCase();
      if (key && !seen.has(key)) seen.set(key, t.trim());
    }
    return Array.from(seen.values()).sort((a, b) => a.localeCompare(b));
  })();
  const selectedTagsLower = selectedTags.map((t) => t.toLowerCase());
  const toggleTag = (t: string) =>
    setSelectedTags((cur) =>
      cur.some((x) => x.toLowerCase() === t.toLowerCase())
        ? cur.filter((x) => x.toLowerCase() !== t.toLowerCase())
        : [...cur, t]
    );
  const filteredProducts = products
    .filter((p) => kindFilter === "all" || (p.product_kind || "").toLowerCase() === kindFilter)
    .filter((p) =>
      selectedTagsLower.length === 0 ||
      (p.tags || []).some((pt) => selectedTagsLower.includes(pt.toLowerCase()))
    );
  // Group-by-tag buckets: a product with N tags appears under each; untagged
  // products fall into a trailing "Untagged" bucket.
  const UNTAGGED = "— Untagged —";
  const groupedProducts: Array<[string, Product[]]> = (() => {
    if (!groupByTag) return [];
    const buckets = new Map<string, Product[]>();
    for (const p of filteredProducts) {
      const tags = (p.tags || []).filter((t) => t.trim());
      if (tags.length === 0) {
        (buckets.get(UNTAGGED) ?? buckets.set(UNTAGGED, []).get(UNTAGGED)!).push(p);
        continue;
      }
      for (const t of tags) {
        const label = t.trim();
        (buckets.get(label) ?? buckets.set(label, []).get(label)!).push(p);
      }
    }
    return Array.from(buckets.entries()).sort(([a], [b]) => {
      if (a === UNTAGGED) return 1;
      if (b === UNTAGGED) return -1;
      return a.localeCompare(b);
    });
  })();

  const renderProductCard = (p: Product) => (
    <div
      key={p.uri}
      onClick={() => selectProduct(p.uri)}
      style={{
        ...styles.card,
        borderColor: selected === p.uri ? "#3b82f6" : "#e2e8f0",
        backgroundColor: selected === p.uri ? "#eff6ff" : "#fff",
      }}
    >
      <div style={styles.cardHeader}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap", minWidth: 0 }}>
          <span style={styles.cardName}>{p.name}</span>
          <ProductKindChip kind={p.product_kind} compact />
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          {p.osi_band && (
            <OsiBadge
              band={p.osi_band}
              completeness={p.osi_completeness}
              conformancePass={p.osi_conformance_pass}
              size="sm"
              showLabel={false}
              rubricShortLabel={p.rubric_short_label}
            />
          )}
          <span style={{
            ...styles.publishedBadge,
            backgroundColor: p.lifecycle_state === "published" ? "#dcfce7" : "#fef3c7",
            color: p.lifecycle_state === "published" ? "#16a34a" : "#92400e",
          }}>
            {p.lifecycle_state === "published" ? "Deployed" : "Coming Soon"}
          </span>
        </div>
      </div>
      {p.description && (
        <div style={styles.cardDesc}>{p.description.slice(0, 120)}{p.description.length > 120 ? "..." : ""}</div>
      )}
      {p.purpose && p.lifecycle_state !== "published" && (
        <div style={{ fontSize: 12, color: "#64748b", marginBottom: 8, fontStyle: "italic" }}>{p.purpose.slice(0, 100)}{p.purpose.length > 100 ? "..." : ""}</div>
      )}
      {p.tags && p.tags.length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginBottom: 8 }}>
          {p.tags.map((t, i) => {
            const on = selectedTagsLower.includes(t.toLowerCase());
            return (
              <span
                key={i}
                onClick={(e) => { e.stopPropagation(); toggleTag(t); }}
                style={{
                  ...styles.tagChip,
                  cursor: "pointer",
                  backgroundColor: on ? "#0f172a" : "#f1f5f9",
                  color: on ? "#fff" : "#475569",
                }}
                title={on ? "Remove tag filter" : "Filter by this tag"}
              >
                {t}
              </span>
            );
          })}
        </div>
      )}
      <div style={styles.cardMeta}>
        {p.owner_name && <span>{p.owner_name}</span>}
        <span>{p.column_count} columns</span>
        {p.published_at ? (
          <span>{new Date(p.published_at).toLocaleDateString()}</span>
        ) : p.created_at ? (
          <span>Created {new Date(p.created_at).toLocaleDateString()}</span>
        ) : null}
      </div>
    </div>
  );

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 20 }}>
        <div>
          <h2 style={{ margin: 0, color: "#0f172a" }}>Data Marketplace</h2>
          <p style={{ margin: "4px 0 0", color: "#64748b", fontSize: 14 }}>
            Browse published data products available for consumption.
          </p>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <button
            onClick={() => setChatOpen(true)}
            style={{ padding: "8px 18px", borderRadius: 6, border: "none", backgroundColor: "#3b82f6", color: "#fff", fontWeight: 600, fontSize: 13, cursor: "pointer" }}
            title="Semantic Q&A — ask a free-form question across deployed views in a domain"
          >
            Semantic Q&amp;A
          </button>
          <button
            onClick={handleExportMarketplaceOkf}
            disabled={exportingMarketplaceOkf}
            style={{ padding: "8px 18px", borderRadius: 6, border: "1px solid #cbd5e1", backgroundColor: "#fff", color: "#475569", fontWeight: 600, fontSize: 13, cursor: exportingMarketplaceOkf ? "wait" : "pointer", opacity: exportingMarketplaceOkf ? 0.6 : 1 }}
            title="Export the whole marketplace as one cross-linked Open Knowledge Format bundle — products built on each other become navigable links"
          >
            {exportingMarketplaceOkf ? "Exporting..." : "Export Open Knowledge Format"}
          </button>
          <button
            onClick={() => navigate("/")}
            style={{ padding: "8px 18px", borderRadius: 6, border: "1px solid #cbd5e1", backgroundColor: "#fff", color: "#475569", fontWeight: 600, fontSize: 13, cursor: "pointer" }}
          >
            Back to Projects
          </button>
        </div>
      </div>

      {/* Sub-tabs: published Data Products vs discovered Datasets. */}
      <div style={{ display: "flex", gap: 4, borderBottom: "2px solid #e2e8f0", marginBottom: 18 }}>
        {([["products", "Data Products"], ["datasets", "Datasets"], ["lineage", "Lineage"], ["flow", "Sankey view"]] as const).map(([key, label]) => (
          <button key={key} onClick={() => setCatalogView(key)}
            style={{ padding: "10px 20px", border: "none", background: "transparent",
              borderBottom: "2px solid " + (catalogView === key ? "#3b82f6" : "transparent"), marginBottom: -2,
              color: catalogView === key ? "#1e293b" : "#64748b", fontWeight: catalogView === key ? 700 : 500,
              fontSize: 14, cursor: "pointer" }}>
            {label}
          </button>
        ))}
      </div>

      {catalogView === "datasets" && <DatasetsCatalog />}

      {catalogView === "lineage" && (
        <ProductLineageView
          onSelect={(uri) => { setCatalogView("products"); selectProduct(uri); }}
        />
      )}

      {catalogView === "flow" && (
        <FlowSankeyCatalogTab
          onOpenNode={(uri) => { setCatalogView("products"); selectProduct(uri); }}
        />
      )}

      {catalogView === "products" && (products.length === 0 ? (
        <div style={styles.emptyState}>
          <div style={{ fontSize: 48, marginBottom: 12 }}>~</div>
          <h3 style={{ margin: "0 0 8px", color: "#334155" }}>No data products yet</h3>
          <p style={{ margin: 0, color: "#94a3b8", maxWidth: 400, lineHeight: 1.5 }}>
            Create a project using the full DPE workflow, define a data contract,
            and generate a data product to see it listed here.
          </p>
        </div>
      ) : (
        <div style={{ display: "grid", gridTemplateColumns: selected ? "340px 1fr" : "repeat(auto-fill, minmax(300px, 1fr))", gap: 16 }}>
          {/* Product cards */}
          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            {/* Source/Consumer filter chips. Counts come from the unfiltered
                set so toggling never hides "all" itself. Client-side filter
                so the chip change is instant. */}
            <KindFilterChips
              filter={kindFilter}
              onChange={setKindFilter}
              counts={{
                all: products.length,
                source: products.filter((p) => (p.product_kind || "").toLowerCase() === "source").length,
                aggregate: products.filter((p) => (p.product_kind || "").toLowerCase() === "aggregate").length,
                consumer: products.filter((p) => (p.product_kind || "").toLowerCase() === "consumer").length,
              }}
            />
            {/* Tag filter (multi-select, OR) + group-by-tag toggle. Only shown
                once at least one product carries a tag. */}
            {allTags.length > 0 && (
              <div style={{ display: "flex", flexDirection: "column", gap: 6, marginBottom: 4 }}>
                <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
                  <span style={{ fontSize: 11, fontWeight: 600, color: "#64748b" }}>Tags:</span>
                  {allTags.map((t) => {
                    const on = selectedTagsLower.includes(t.toLowerCase());
                    return (
                      <button
                        key={t}
                        type="button"
                        onClick={() => toggleTag(t)}
                        style={{
                          padding: "3px 9px", borderRadius: 999, fontSize: 11, fontWeight: 600, cursor: "pointer",
                          backgroundColor: on ? "#0f172a" : "#fff", color: on ? "#fff" : "#475569",
                          border: `1px solid ${on ? "#0f172a" : "#cbd5e1"}`, whiteSpace: "nowrap",
                        }}
                      >
                        {t}{on ? " ✕" : ""}
                      </button>
                    );
                  })}
                  {selectedTags.length > 0 && (
                    <button
                      type="button"
                      onClick={() => setSelectedTags([])}
                      style={{ padding: "3px 8px", borderRadius: 999, fontSize: 11, fontWeight: 500, cursor: "pointer", background: "transparent", color: "#94a3b8", border: "none", textDecoration: "underline" }}
                    >
                      clear
                    </button>
                  )}
                </div>
                <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, color: "#475569", cursor: "pointer", userSelect: "none" }}>
                  <input type="checkbox" checked={groupByTag} onChange={(e) => setGroupByTag(e.target.checked)} />
                  Group by tag
                </label>
              </div>
            )}
            {groupByTag ? (
              groupedProducts.length === 0 ? (
                <div style={{ fontSize: 13, color: "#94a3b8", padding: "8px 4px" }}>No products match the current filters.</div>
              ) : (
                groupedProducts.map(([tagName, bucket]) => (
                  <div key={tagName} style={{ display: "flex", flexDirection: "column", gap: 12 }}>
                    <div style={{ fontSize: 12, fontWeight: 700, color: "#475569", textTransform: "uppercase", letterSpacing: 0.4, marginTop: 4 }}>
                      {tagName} <span style={{ opacity: 0.6, fontWeight: 500 }}>({bucket.length})</span>
                    </div>
                    {bucket.map(renderProductCard)}
                  </div>
                ))
              )
            ) : (
              filteredProducts.length === 0 ? (
                <div style={{ fontSize: 13, color: "#94a3b8", padding: "8px 4px" }}>No products match the current filters.</div>
              ) : (
                filteredProducts.map(renderProductCard)
              )
            )}
          </div>

          {/* Detail panel */}
          {selected && detail && (
            <div style={styles.detailPanel}>
              {showDeployChecklist && detail.project_id && (
                <DeployChecklistModal
                  projectId={detail.project_id}
                  deploying={deploying}
                  onDeploy={handleDeploy}
                  onEditDetails={() =>
                    // Edit-mode wizard (the `edit/:projectId` route); ?step is a
                    // route-independent query param. There is no `new/consumer/:id` route.
                    navigate(`/product/edit/${detail.project_id}?step=6`)
                  }
                  onClose={() => setShowDeployChecklist(false)}
                />
              )}
              <div style={styles.detailHeader}>
                <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", minWidth: 0 }}>
                  <h3 style={{ margin: 0, color: "#0f172a" }}>{detail.title || detail.name}</h3>
                  <ProductKindChip kind={detail.product_kind} />
                  {/* Phase 1 versioning: dropdown across all :ContractVersion
                      sidecars. Picking a version re-fetches detail with
                      ?version=N for historical rendering. */}
                  {detail.available_versions && detail.available_versions.length > 1 && (
                    <select
                      value={selectedVersion ?? ""}
                      onChange={(e) => {
                        const v = e.target.value === "" ? null : parseInt(e.target.value, 10);
                        selectProduct(detail.uri, v);
                      }}
                      style={{
                        fontSize: 13,
                        padding: "4px 8px",
                        borderRadius: 6,
                        border: "1px solid #d1d5db",
                        background: "white",
                      }}
                      title="View the product as it looked at a historical version"
                    >
                      <option value="">Latest deployed (auto)</option>
                      {detail.available_versions.map((v) => (
                        <option key={v.version} value={v.version}>
                          v{v.version} ({v.lifecycle_state}){v.is_current ? " — current" : ""}
                        </option>
                      ))}
                    </select>
                  )}
                  {selectedVersion !== null && (
                    <span
                      style={{
                        padding: "2px 8px",
                        background: "#fef3c7",
                        color: "#92400e",
                        borderRadius: 12,
                        fontSize: 11,
                        fontWeight: 600,
                      }}
                    >
                      Viewing v{selectedVersion} (historical)
                    </span>
                  )}
                </div>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  {detail.has_inflight_edit && (
                    <span style={styles.editInFlightBadge}>Edit in progress</span>
                  )}
                  {detail.contract_id && (
                    <button
                      onClick={() => handleGenerateReport(detail)}
                      disabled={generatingReport}
                      style={{
                        ...styles.reportBtn,
                        opacity: generatingReport ? 0.6 : 1,
                        cursor: generatingReport ? "wait" : "pointer",
                      }}
                      title="Download a Markdown snapshot report (with Mermaid diagrams) for this product"
                    >
                      {generatingReport ? "Generating..." : "Generate Report"}
                    </button>
                  )}
                  {detail.contract_id && (
                    <button
                      onClick={() => handleDownloadOdcsYaml(detail)}
                      disabled={downloadingYaml}
                      style={{
                        ...styles.reportBtn,
                        opacity: downloadingYaml ? 0.6 : 1,
                        cursor: downloadingYaml ? "wait" : "pointer",
                      }}
                      title="Download the canonical ODCS v3.1 YAML contract for this product"
                    >
                      {downloadingYaml ? "Downloading..." : "Download ODCS YAML"}
                    </button>
                  )}
                  {detail.contract_id && (
                    <button
                      onClick={() => handleExportOkf(detail)}
                      disabled={downloadingOkf}
                      style={{
                        ...styles.reportBtn,
                        opacity: downloadingOkf ? 0.6 : 1,
                        cursor: downloadingOkf ? "wait" : "pointer",
                      }}
                      title="Export an Open Knowledge Format bundle — cross-linked markdown + YAML frontmatter for an external AI agent (a portable briefing alongside ODCS)"
                    >
                      {downloadingOkf ? "Exporting..." : "Export Open Knowledge Format"}
                    </button>
                  )}
                  {detail.contract_id && (
                    <button
                      onClick={() => handleCopyOdcsUrl(detail)}
                      style={styles.copyUrlBtn}
                      title="Copy the stable ODCS YAML URL — paste into curl, CI configs, or downstream catalog imports"
                    >
                      {copyToast ? "Copied" : "Copy URL"}
                    </button>
                  )}
                  {canDeploy(detail) && (
                    <button
                      onClick={onDeployClick}
                      disabled={deploying}
                      style={{
                        ...styles.deployBtn,
                        opacity: deploying ? 0.6 : 1,
                        cursor: deploying ? "wait" : "pointer",
                      }}
                    >
                      {deploying
                        ? "Deploying..."
                        : detail.lifecycle_state === "published"
                        ? "Deploy update"
                        : "Deploy"}
                    </button>
                  )}
                  <button onClick={() => setSelected(null)} style={styles.closeBtn}>x</button>
                </div>
              </div>

              {/* Soft reclassify nudge: a 'consumer' leaf that other products
                  now build on is really an aggregate. No auto-change — the PO
                  reclassifies via the wizard's Product-Details intent step. */}
              {detail.product_kind === "consumer" && detail.consumed_by.length > 0 && (
                <div
                  style={{
                    margin: "10px 0",
                    padding: "8px 12px",
                    borderRadius: 8,
                    backgroundColor: "#ede9fe",
                    color: "#6d28d9",
                    border: "1px solid #c4b5fd",
                    fontSize: 12,
                  }}
                >
                  This product is consumed by {detail.consumed_by.length} other{" "}
                  {detail.consumed_by.length === 1 ? "product" : "products"}. It's acting as a
                  reusable building block — consider reclassifying it as an <strong>Aggregate</strong>{" "}
                  (edit the product → Product Details → Product intent).
                </div>
              )}

              {/* Detail tabs — grouped: ODCS-spec sections first, then a
                  divider, then the additional (non-contract) views. */}
              <div style={styles.detailTabBar}>
                {([
                  { key: "overview", group: "spec" },
                  { key: "schema", group: "spec" },
                  { key: "quality", group: "spec" },
                  { key: "servers", group: "spec" },
                  { key: "terms", group: "spec" },
                  { key: "team", group: "spec" },
                  { key: "roles", group: "spec" },
                  { key: "osi", group: "extra" },
                  { key: "qa", group: "extra" },
                  { key: "lineage", group: "extra" },
                  { key: "serving", group: "extra" },
                  { key: "preview", group: "extra" },
                  { key: "reflection", group: "extra" },
                ] as const).map((item, i, arr) => {
                  const t = item.key;
                  const showDivider = i > 0 && arr[i - 1].group !== item.group;
                  const label = t === "osi" ? (detail.rubric_short_label || "OSI")
                    : t === "qa" ? "Q&A" : t === "terms" ? "SLA"
                    : t.charAt(0).toUpperCase() + t.slice(1);
                  return (
                    <span key={t} style={{ display: "contents" }}>
                      {showDivider && (
                        <span
                          style={styles.detailTabDivider}
                          title="Additional views — not part of the ODCS contract"
                        />
                      )}
                      <button
                        onClick={() => setDetailTab(t)}
                        style={{
                          ...styles.detailTab,
                          borderBottomColor: detailTab === t ? "#3b82f6" : "transparent",
                          color: detailTab === t ? "#1e293b" : "#64748b",
                          fontWeight: detailTab === t ? 700 : 500,
                        }}
                      >
                        {label}
                      </button>
                    </span>
                  );
                })}
              </div>

              <div style={styles.detailContent}>
                {detailTab === "overview" && (
                  <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
                    {detail.contract_id && (
                      <RevisionTimeline
                        contractId={detail.contract_id}
                        selectedVersion={selectedVersion}
                        onSelectVersion={(v) => selectProduct(detail.uri, v)}
                      />
                    )}
                    {detail.description && <Section label="Description" value={detail.description} />}
                    {detail.purpose && <Section label="Purpose" value={detail.purpose} />}
                    {detail.limitations && <Section label="Limitations" value={detail.limitations} />}
                    <div style={{ display: "grid", gridTemplateColumns: "repeat(5, 1fr)", gap: 12 }}>
                      <StatCard label="Datasets" value={String((detail.datasets || []).filter(d => d.physicalName).length)} color="#8b5cf6" />
                      <StatCard label="Columns" value={String((detail.datasets || []).reduce((n, d) => n + (d.columns?.length || 0), 0))} color="#3b82f6" />
                      <StatCard label="Quality Rules" value={String(detail.quality_rules?.length || 0)} color="#22c55e" />
                      <StatCard label="Source Tables" value={String(lineage?.sources?.length || 0)} color="#f59e0b" />
                      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                        <OsiStatCard
                          band={detail.osi_band}
                          completeness={detail.osi_completeness}
                          conformancePass={detail.osi_conformance_pass}
                          rubricShortLabel={detail.rubric_short_label}
                        />
                        {canScoreOsi(detail) && (
                          <button
                            type="button"
                            onClick={handleScoreOsi}
                            disabled={scoringOsi}
                            style={{
                              padding: "4px 8px",
                              borderRadius: 5,
                              fontSize: 11,
                              fontWeight: 600,
                              border: "1px solid #cbd5e1",
                              backgroundColor: scoringOsi ? "#f1f5f9" : "#fff",
                              color: "#334155",
                              cursor: scoringOsi ? "wait" : "pointer",
                            }}
                            title="Re-run OSI evaluation against the current contract"
                          >
                            {scoringOsi ? "Scoring..." : "Score OSI now"}
                          </button>
                        )}
                      </div>
                    </div>
                    {(detail.domain || detail.data_product) && (
                      <div style={{ display: "flex", gap: 16, fontSize: 13, color: "#475569" }}>
                        {detail.domain && <div><span style={{ color: "#94a3b8" }}>Domain:</span> <strong>{detail.domain}</strong></div>}
                        {detail.data_product && <div><span style={{ color: "#94a3b8" }}>Data Product:</span> <strong>{detail.data_product}</strong></div>}
                      </div>
                    )}
                    {/* Cross-product relationships. Consumer products show
                        their inputs ("Consumes"); source products show the
                        downstream consumers ("Consumed by"). Both navigate
                        in-place via selectProduct so the user can pivot
                        across the dependency graph without leaving the
                        marketplace. */}
                    {(detail.consumes && detail.consumes.length > 0) && (
                      <div>
                        <div style={styles.sectionLabel}>Consumes ({detail.consumes.length})</div>
                        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                          {detail.consumes.map((c) => (
                            <button
                              key={c.uri}
                              type="button"
                              onClick={() => selectProduct(c.uri)}
                              style={crossRefRowStyle}
                            >
                              <span style={{ fontWeight: 600, color: "#0f172a" }}>{c.name || c.uri}</span>
                              <ProductKindChip kind={c.product_kind} compact />
                              <span style={{ color: "#94a3b8", fontSize: 11, marginLeft: "auto" }}>Open →</span>
                            </button>
                          ))}
                        </div>
                      </div>
                    )}
                    {(detail.consumed_by && detail.consumed_by.length > 0) && (
                      <div>
                        <div style={styles.sectionLabel}>Consumed by ({detail.consumed_by.length})</div>
                        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                          {detail.consumed_by.map((c) => (
                            <button
                              key={c.uri}
                              type="button"
                              onClick={() => selectProduct(c.uri)}
                              style={crossRefRowStyle}
                            >
                              <span style={{ fontWeight: 600, color: "#0f172a" }}>{c.name || c.uri}</span>
                              <ProductKindChip kind={c.product_kind} compact />
                              {c.lifecycle_state && c.lifecycle_state !== "published" && (
                                <span style={{
                                  padding: "1px 6px", borderRadius: 999, fontSize: 10, fontWeight: 600,
                                  backgroundColor: "#fef3c7", color: "#92400e", border: "1px solid #fde68a",
                                }}>
                                  {c.lifecycle_state}
                                </span>
                              )}
                              <span style={{ color: "#94a3b8", fontSize: 11, marginLeft: "auto" }}>Open →</span>
                            </button>
                          ))}
                        </div>
                      </div>
                    )}
                    {(( detail.tags && detail.tags.length > 0) || canEditTags(detail)) && (
                      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                          <span style={{ fontSize: 11, fontWeight: 700, color: "#64748b", textTransform: "uppercase", letterSpacing: 0.4 }}>Tags</span>
                          {canEditTags(detail) && !editingTags && (
                            <button
                              type="button"
                              onClick={beginEditTags}
                              style={{ border: "none", background: "transparent", color: "#3b82f6", cursor: "pointer", fontSize: 12, fontWeight: 600, padding: 0 }}
                            >
                              {detail.tags && detail.tags.length > 0 ? "Edit" : "+ Add tags"}
                            </button>
                          )}
                        </div>
                        {!editingTags ? (
                          detail.tags && detail.tags.length > 0 ? (
                            <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                              {detail.tags.map((t, i) => (
                                <span key={i} style={styles.tagChip}>{t}</span>
                              ))}
                            </div>
                          ) : (
                            <span style={{ fontSize: 12, color: "#94a3b8" }}>No tags yet.</span>
                          )
                        ) : (
                          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                            {tagsDraft.length > 0 && (
                              <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                                {tagsDraft.map((t) => (
                                  <span key={t} style={{ display: "inline-flex", alignItems: "center", gap: 4, padding: "2px 8px", borderRadius: 999, fontSize: 12, fontWeight: 600, backgroundColor: "#eef2ff", color: "#4338ca", border: "1px solid #c7d2fe" }}>
                                    {t}
                                    <button type="button" onClick={() => setTagsDraft((cur) => cur.filter((x) => x !== t))} style={{ border: "none", background: "transparent", color: "#6366f1", cursor: "pointer", fontSize: 13, lineHeight: 1, padding: 0 }} title="Remove tag">✕</button>
                                  </span>
                                ))}
                              </div>
                            )}
                            <input
                              value={tagDraftInput}
                              onChange={(e) => setTagDraftInput(e.target.value)}
                              onKeyDown={(e) => {
                                if (e.key === "Enter" || e.key === ",") { e.preventDefault(); addTagDraft(tagDraftInput); }
                                else if (e.key === "Backspace" && tagDraftInput === "" && tagsDraft.length > 0) { setTagsDraft((cur) => cur.slice(0, -1)); }
                              }}
                              placeholder="Type a tag, press Enter"
                              style={{ padding: "6px 10px", borderRadius: 6, border: "1px solid #cbd5e1", fontSize: 13 }}
                            />
                            <div style={{ display: "flex", gap: 8 }}>
                              <button type="button" onClick={handleSaveTags} disabled={savingTags}
                                style={{ padding: "5px 12px", borderRadius: 6, border: "none", backgroundColor: "#3b82f6", color: "#fff", fontWeight: 600, fontSize: 12, cursor: savingTags ? "wait" : "pointer", opacity: savingTags ? 0.6 : 1 }}>
                                {savingTags ? "Saving…" : "Save tags"}
                              </button>
                              <button type="button" onClick={() => setEditingTags(false)} disabled={savingTags}
                                style={{ padding: "5px 12px", borderRadius: 6, border: "1px solid #cbd5e1", backgroundColor: "#fff", color: "#475569", fontWeight: 600, fontSize: 12, cursor: "pointer" }}>
                                Cancel
                              </button>
                            </div>
                          </div>
                        )}
                      </div>
                    )}
                    {(detail.owners?.length || detail.stewards?.length) ? (
                      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
                        {detail.owners && detail.owners.length > 0 && (
                          <div>
                            <div style={styles.sectionLabel}>Owners ({detail.owners.length})</div>
                            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                              {detail.owners.map((o, i) => (
                                <div key={i} style={styles.ownerBlock}>
                                  <span style={styles.ownerAvatar}>
                                    {(o.name || o.username || "?").split(/[\s@]/).filter(Boolean).slice(0, 2).map(n => n[0]).join("").toUpperCase()}
                                  </span>
                                  <div>
                                    <div style={{ fontWeight: 600, fontSize: 13 }}>{o.name || o.username}</div>
                                    <div style={{ fontSize: 12, color: "#64748b" }}>{o.role}{o.email ? ` · ${o.email}` : ""}</div>
                                  </div>
                                </div>
                              ))}
                            </div>
                          </div>
                        )}
                        {detail.stewards && detail.stewards.length > 0 && (
                          <div>
                            <div style={styles.sectionLabel}>Stewards ({detail.stewards.length})</div>
                            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                              {detail.stewards.map((s, i) => (
                                <div key={i} style={styles.ownerBlock}>
                                  <span style={{ ...styles.ownerAvatar, backgroundColor: "#8b5cf6" }}>
                                    {(s.name || s.username || "?").split(/[\s@]/).filter(Boolean).slice(0, 2).map(n => n[0]).join("").toUpperCase()}
                                  </span>
                                  <div>
                                    <div style={{ fontWeight: 600, fontSize: 13 }}>{s.name || s.username}</div>
                                    <div style={{ fontSize: 12, color: "#64748b" }}>{s.role}{s.email ? ` · ${s.email}` : ""}</div>
                                  </div>
                                </div>
                              ))}
                            </div>
                          </div>
                        )}
                      </div>
                    ) : null}
                    {detail.support && ((detail.support.contacts?.length || 0) > 0 || (detail.support.documentation?.length || 0) > 0 || detail.support.escalationPolicy) && (
                      <div>
                        <div style={styles.sectionLabel}>Support</div>
                        <div style={{ display: "flex", flexDirection: "column", gap: 6, fontSize: 13, color: "#334155" }}>
                          {(detail.support.contacts || []).map((c, i) => (
                            <div key={`c${i}`}><span style={{ color: "#94a3b8" }}>{c.type}:</span> {c.value}</div>
                          ))}
                          {(detail.support.documentation || []).map((d, i) => (
                            <div key={`d${i}`}><span style={{ color: "#94a3b8" }}>{d.type}:</span> {d.url ? <a href={d.url} target="_blank" rel="noreferrer">{d.url}</a> : "—"}</div>
                          ))}
                          {detail.support.escalationPolicy && (
                            <div><span style={{ color: "#94a3b8" }}>Escalation:</span> {detail.support.escalationPolicy}</div>
                          )}
                        </div>
                      </div>
                    )}
                    {/* External catalog placeholder */}
                    <div style={styles.externalCatalog}>
                      <span style={{ fontWeight: 600 }}>Publish to External Catalog</span>
                      <span style={{ fontSize: 12, color: "#94a3b8" }}>Integration available — connect to your enterprise data catalog</span>
                      <button disabled style={styles.catalogBtn}>Configure Catalog Integration</button>
                    </div>
                  </div>
                )}

                {detailTab === "schema" && (
                  <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
                    {(detail.datasets || []).filter(d => d.physicalName).length === 0 && (
                      <div style={{ color: "#94a3b8", fontSize: 13 }}>No datasets defined.</div>
                    )}
                    {(detail.datasets || []).filter(d => d.physicalName).map((d, di) => (
                      <div key={di} style={{ border: "1px solid #e2e8f0", borderRadius: 8, overflow: "hidden" }}>
                        <div style={{ padding: "10px 14px", backgroundColor: "#f8fafc", borderBottom: "1px solid #e2e8f0" }}>
                          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                            <span style={{ fontWeight: 700, fontSize: 14, color: "#0f172a" }}>{d.name || d.physicalName}</span>
                            {d.name && d.physicalName && d.name !== d.physicalName && (
                              <span style={{ fontSize: 12, color: "#94a3b8" }}><code>{d.physicalName}</code></span>
                            )}
                            <RelationshipKindChip kind={d.relationshipKind} long />
                            <span style={{ fontSize: 11, color: "#64748b" }}>{d.columns?.length || 0} columns</span>
                          </div>
                          {d.description && <div style={{ fontSize: 12, color: "#64748b", marginTop: 4 }}>{d.description}</div>}
                        </div>
                        <table style={styles.table}>
                          <thead>
                            <tr>
                              <th style={styles.th}>Property</th>
                              <th style={styles.th}>Logical Type</th>
                              <th style={styles.th}>Physical Type</th>
                              <th style={styles.th}>PK</th>
                              <th style={styles.th}>Description</th>
                            </tr>
                          </thead>
                          <tbody>
                            {(d.columns || []).map((c, i) => (
                              <tr key={i} style={{ backgroundColor: i % 2 === 0 ? "#fff" : "#f8fafc" }}>
                                <td style={styles.td}>
                                  <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
                                    <span style={{ fontWeight: 600 }}>{c.name}</span>
                                    <SensitivityChip sensitivity={c.sensitivity} compact />
                                  </div>
                                  {c.logicalName && <div style={{ fontSize: 11, color: "#94a3b8" }}>{c.logicalName}</div>}
                                </td>
                                <td style={styles.td}><code style={{ fontSize: 12, color: "#64748b" }}>{c.logicalType || "—"}</code></td>
                                <td style={styles.td}><code style={{ fontSize: 12, color: "#6366f1" }}>{c.physicalType || "—"}</code></td>
                                <td style={styles.td}>{c.primaryKey ? <span style={styles.pkBadge}>PK</span> : ""}</td>
                                <td style={{ ...styles.td, color: "#64748b" }}>{c.description || "—"}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    ))}
                  </div>
                )}

                {detailTab === "quality" && (
                  <MarketplaceQualityTab rules={detail.quality_rules || []} />
                )}

                {detailTab === "osi" && (
                  detail.project_id ? (
                    <OsiAnalysisPanel
                      projectId={detail.project_id}
                      scoreOnDemand={canScoreOsi(detail)}
                    />
                  ) : (
                    <div style={{ padding: 16, color: "#64748b", fontSize: 13 }}>
                      OSI evaluation requires a project context — this product has no resolvable project_id.
                    </div>
                  )
                )}

                {detailTab === "qa" && (
                  <QuestionsPanel
                    projectId={detail.project_id ?? null}
                    marketplaceUri={detail.uri}
                    initial={detail.qa_evaluation || null}
                    canRegenerate={detail.project_id !== null && canScoreOsi(detail)}
                    canProbe
                    onRegenerated={() => {
                      // Refresh detail so the persisted eval becomes the new
                      // baseline (also picks up the cleared `stale` flag).
                      selectProduct(detail.uri, selectedVersion);
                    }}
                  />
                )}

                {detailTab === "team" && (
                  <div>
                    {(detail.team || []).length === 0 ? (
                      <div style={{ color: "#94a3b8", fontSize: 13 }}>No team members defined.</div>
                    ) : (
                      <table style={styles.table}>
                        <thead>
                          <tr>
                            <th style={styles.th}>Username</th>
                            <th style={styles.th}>Name</th>
                            <th style={styles.th}>Role</th>
                            <th style={styles.th}>Email</th>
                          </tr>
                        </thead>
                        <tbody>
                          {(detail.team || []).map((m, i) => (
                            <tr key={i} style={{ backgroundColor: i % 2 === 0 ? "#fff" : "#f8fafc" }}>
                              <td style={styles.td}><code style={{ fontSize: 12 }}>{m.username || "—"}</code></td>
                              <td style={styles.td}>{m.name || "—"}</td>
                              <td style={styles.td}>{m.role || "—"}</td>
                              <td style={styles.td}>{m.email || "—"}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    )}
                  </div>
                )}

                {detailTab === "roles" && (
                  <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                    {(detail.roles || []).length === 0 && (
                      <div style={{ color: "#94a3b8", fontSize: 13 }}>No access roles defined.</div>
                    )}
                    {(detail.roles || []).map((r, i) => (
                      <div key={i} style={{ padding: 12, borderRadius: 8, border: "1px solid #e2e8f0", backgroundColor: "#fff" }}>
                        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
                          <span style={{ fontWeight: 700, fontSize: 14 }}>{r.role}</span>
                          {r.access && <span style={{ ...styles.tagChip, backgroundColor: "#eef2ff", color: "#4f46e5" }}>{r.access}</span>}
                        </div>
                        {r.description && <div style={{ fontSize: 13, color: "#334155", marginBottom: 6 }}>{r.description}</div>}
                        {r.datasets && r.datasets.length > 0 && (
                          <div style={{ fontSize: 12, color: "#64748b" }}>
                            <span style={{ color: "#94a3b8" }}>Datasets:</span>{" "}
                            {r.datasets.map((d, di) => (
                              <code key={di} style={{ marginRight: 6 }}>{d}</code>
                            ))}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                )}

                {detailTab === "servers" && (
                  <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
                    {(detail.servers || []).length === 0 && (
                      <div style={{ color: "#94a3b8", fontSize: 13 }}>No servers registered.</div>
                    )}
                    {(detail.servers || []).map((srv, i) => (
                      <div key={i} style={{ border: "1px solid #e2e8f0", borderRadius: 8, overflow: "hidden" }}>
                        <div style={{ padding: "10px 14px", backgroundColor: "#f8fafc", borderBottom: "1px solid #e2e8f0", display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
                          <span style={{ fontWeight: 700, fontSize: 14 }}>{srv.name || "(unnamed)"}</span>
                          {srv.environment && <span style={{ ...styles.tagChip, backgroundColor: "#f0fdf4", color: "#15803d" }}>{srv.environment}</span>}
                          {srv.type && <span style={styles.tagChip}>{srv.type}</span>}
                        </div>
                        <div style={{ padding: 12, display: "grid", gridTemplateColumns: "auto 1fr", columnGap: 12, rowGap: 4, fontSize: 13 }}>
                          {srv.account && <><span style={{ color: "#94a3b8" }}>Account</span><code>{srv.account}</code></>}
                          {srv.database && <><span style={{ color: "#94a3b8" }}>Database</span><code>{srv.database}</code></>}
                          {srv.schema && <><span style={{ color: "#94a3b8" }}>Schema</span><code>{srv.schema}</code></>}
                          {srv.datasets && srv.datasets.length > 0 && (
                            <>
                              <span style={{ color: "#94a3b8" }}>Datasets</span>
                              <pre style={{ margin: 0, padding: 8, backgroundColor: "#f1f5f9", borderRadius: 6, fontSize: 11, whiteSpace: "pre-wrap", overflow: "auto", maxHeight: 200 }}>
                                {JSON.stringify(srv.datasets, null, 2)}
                              </pre>
                            </>
                          )}
                        </div>
                      </div>
                    ))}
                  </div>
                )}

                {detailTab === "lineage" && lineage && (
                  <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
                    {/* Stats */}
                    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 12 }}>
                      <StatCard label="Product Columns" value={String(lineage.stats.total_columns)} color="#3b82f6" />
                      <StatCard label="Mapped Columns" value={String(lineage.stats.total_mappings)} color="#22c55e" />
                      <StatCard label="Quality Rules" value={String(lineage.stats.total_quality_rules)} color="#ef4444" />
                    </div>
                    {/* Source tables */}
                    <div>
                      <div style={{ fontSize: 13, fontWeight: 700, color: "#334155", marginBottom: 8 }}>Source Tables</div>
                      {lineage.sources.length === 0 ? (
                        <div style={{ color: "#94a3b8", fontSize: 13 }}>No source mappings found.</div>
                      ) : (
                        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                          {lineage.sources.map((s, i) => (
                            <div key={i} style={styles.lineageRow}>
                              <div>
                                <span style={{ fontWeight: 600, fontSize: 13 }}>{s.source_schema ? `${s.source_schema}.` : ""}{s.source_table}</span>
                                {s.source_system && <span style={{ fontSize: 11, color: "#94a3b8", marginLeft: 8 }}>{s.source_system}</span>}
                                {s.is_lookup && (
                                  <span style={{ fontSize: 10, fontWeight: 700, color: "#9333ea", backgroundColor: "#f3e8ff", padding: "1px 6px", borderRadius: 3, marginLeft: 8 }}>
                                    lookup
                                  </span>
                                )}
                              </div>
                              <div style={{ fontSize: 12, color: "#64748b" }}>
                                {s.is_lookup
                                  ? `${s.lookup_columns ?? 0} lookup column${(s.lookup_columns ?? 0) === 1 ? "" : "s"}`
                                  : `${s.mapped_columns} / ${s.source_columns} columns mapped`}
                              </div>
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                    {/* Column-level mapping graph */}
                    <div>
                      <div style={{ fontSize: 13, fontWeight: 700, color: "#334155", marginBottom: 8 }}>
                        Mapping Graph
                      </div>
                      {mappingGraph ? (
                        <MappingGraphView
                          payload={mappingGraph}
                          readOnly
                          enableHopExpansion
                          productUri={detail.uri}
                          productKind={detail.product_kind ?? undefined}
                        />
                      ) : (
                        <div style={{ fontSize: 13, color: "#94a3b8", padding: 24, textAlign: "center", border: "1px dashed #e2e8f0", borderRadius: 8 }}>
                          No mapping graph available for this product yet.
                        </div>
                      )}
                    </div>
                  </div>
                )}

                {detailTab === "serving" && (
                  <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
                    {detail.project_id && <ServingGitLinks projectId={detail.project_id} />}
                    {detail.serving && detail.serving.filter(s => s.servingMode).length > 0 ? (
                      detail.serving.filter(s => s.servingMode).map((s, i) => (
                        <div key={i} style={{ border: "1px solid #e2e8f0", borderRadius: 8, overflow: "hidden" }}>
                          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "12px 16px", backgroundColor: s.servingMode === "dbt_materialized" ? "#faf5ff" : s.servingMode === "virtual_view" ? "#f0fdf4" : "#f8fafc", borderBottom: "1px solid #e2e8f0" }}>
                            <div>
                              <span style={{ fontSize: 12, fontWeight: 700, color: servingModeColor(s.servingMode), textTransform: "uppercase", letterSpacing: "0.05em" }}>
                                {servingModeLabel(s.servingMode)}
                              </span>
                              {s.servingMode === "dbt_materialized" && s.dbtMaterialization && <span style={{ fontSize: 12, color: "#64748b", marginLeft: 12 }}>{s.dbtMaterialization}</span>}
                              {s.targetPlatform && <span style={{ fontSize: 12, color: "#64748b", marginLeft: 12 }}>{s.targetPlatform}</span>}
                              {s.servingMode === "dbt_materialized" && s.buildStatus && (
                                <span style={{ fontSize: 11, fontWeight: 700, marginLeft: 12, padding: "2px 8px", borderRadius: 999,
                                  background: s.buildStatus === "built" ? "#dcfce7" : "#fee2e2", color: s.buildStatus === "built" ? "#15803d" : "#b91c1c" }}>
                                  {s.buildStatus === "built" ? "BUILT" : s.buildStatus.toUpperCase()}
                                </span>
                              )}
                            </div>
                            {s.servingMode === "dbt_materialized"
                              ? s.targetSchema && <span style={{ fontWeight: 600, fontSize: 14, color: "#0f172a" }}>{s.targetSchema}</span>
                              : s.viewName && <span style={{ fontWeight: 600, fontSize: 14, color: "#0f172a" }}>{s.viewSchema ? `${s.viewSchema}.` : ""}{s.viewName}</span>}
                          </div>
                          {s.servingMode === "dbt_materialized" && (() => {
                            let models: Array<{ model: string; kind?: string; rows?: number | null; ddl?: string | null }> = [];
                            try { models = JSON.parse(s.modelsJson || "[]"); } catch { /* ignore */ }
                            if (models.length === 0) return null;
                            return (
                              <div style={{ padding: "10px 16px", display: "flex", flexDirection: "column", gap: 6 }}>
                                {models.map((m) => {
                                  const open = !!expandedModelSql[m.model];
                                  return (
                                  <div key={m.model} style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                                    <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13 }}>
                                      <span style={{ fontWeight: 600, color: "#0f172a" }}>{m.model}</span>
                                      {m.kind === "snapshot" && (
                                        <span title="dbt snapshot — accrues SCD2 history" style={{ fontSize: 10, fontWeight: 700, color: "#7c3aed", background: "#f3e8ff", borderRadius: 4, padding: "1px 6px" }}>SCD2 SNAPSHOT</span>
                                      )}
                                      {typeof m.rows === "number" && <span style={{ color: "#64748b" }}>· {m.rows.toLocaleString()} rows</span>}
                                      {m.ddl && (
                                        <span
                                          onClick={() => setExpandedModelSql((p) => ({ ...p, [m.model]: !open }))}
                                          style={{ fontSize: 11, color: "#7c3aed", fontWeight: 600, cursor: "pointer", userSelect: "none" }}
                                        >
                                          {open ? "Hide SQL" : "Show SQL"}
                                        </span>
                                      )}
                                    </div>
                                    {open && m.ddl && (
                                      <pre style={{
                                        margin: 0, padding: "10px 12px", backgroundColor: "#0f172a", color: "#a5f3fc",
                                        fontSize: 12, lineHeight: 1.5, overflow: "auto", maxHeight: 360,
                                        fontFamily: "'Fira Code', monospace", whiteSpace: "pre-wrap",
                                      }}>{m.ddl}</pre>
                                    )}
                                  </div>
                                  );
                                })}
                                {s.builtAt && <div style={{ fontSize: 11, color: "#94a3b8" }}>Built {String(s.builtAt).slice(0, 16).replace("T", " ")}</div>}
                              </div>
                            );
                          })()}
                          {s.servingMode === "dbt_materialized" && s.buildStatus === "built" && detail?.project_id && (
                            <div style={{ padding: "4px 16px 10px" }}>
                              <button
                                onClick={() => handleDownloadDbtProject(detail)}
                                disabled={downloadingDbt}
                                style={{ padding: "5px 12px", borderRadius: 6, border: "1px solid #cbd5e1",
                                  background: "#fff", color: "#7c3aed", fontSize: 12, fontWeight: 600,
                                  cursor: downloadingDbt ? "default" : "pointer" }}
                                title="Download the generated dbt project (models, macros, dbt_project.yml) to run/maintain it yourself"
                              >
                                {downloadingDbt ? "Preparing…" : "⤓ Download dbt project"}
                              </button>
                            </div>
                          )}
                          {s.servingMode === "dbt_materialized" && s.buildError && (
                            <div style={{ padding: "8px 16px", fontSize: 12, color: "#b91c1c" }}>{s.buildError}</div>
                          )}
                          {s.summaryJson && (
                            <div style={{ padding: "8px 16px" }}>
                              <ServingWarningsCallout summaryJson={s.summaryJson} defaultOpen />
                            </div>
                          )}
                          {s.ddl && (
                            <div>
                              <div
                                onClick={() => setDdlExpanded(!ddlExpanded)}
                                style={{ padding: "8px 16px", cursor: "pointer", fontSize: 12, color: "#059669", fontWeight: 600, userSelect: "none" }}
                              >
                                {ddlExpanded ? "Hide DDL" : "Show DDL"}
                              </div>
                              {ddlExpanded && (
                                <pre style={{
                                  margin: 0, padding: "12px 16px", backgroundColor: "#0f172a", color: "#a5f3fc",
                                  fontSize: 12, lineHeight: 1.5, overflow: "auto", maxHeight: 400,
                                  fontFamily: "'Fira Code', monospace", whiteSpace: "pre-wrap",
                                }}>
                                  {s.ddl}
                                </pre>
                              )}
                            </div>
                          )}
                        </div>
                      ))
                    ) : (
                      <div style={{ color: "#94a3b8", fontSize: 13 }}>No serving definition configured yet.</div>
                    )}
                  </div>
                )}

                {detailTab === "preview" && detail.contract_id && (
                  <PreviewTab
                    endpoint={`/api/marketplace/products/${detail.contract_id}/preview`}
                    datasets={(detail.datasets || [])
                      .filter(d => d.physicalName)
                      .map(d => ({
                        uri: d.uri || undefined,
                        physicalName: d.physicalName || undefined,
                        name: d.name || undefined,
                        description: d.description,
                        relationshipKind: d.relationshipKind,
                      }))}
                    limit={50}
                  />
                )}

                {detailTab === "reflection" && detail.contract_id && (
                  <DeploymentReflectionPanel contractId={detail.contract_id} />
                )}

                {detailTab === "terms" && (
                  <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
                    {detail.terms_usage && <Section label="Usage Terms" value={detail.terms_usage} />}
                    {detail.terms_limitations && <Section label="Limitations" value={detail.terms_limitations} />}
                    {detail.terms_notice_period && <Section label="Notice Period" value={detail.terms_notice_period} />}
                    {detail.slas && detail.slas.length > 0 && (
                      <div>
                        <div style={styles.sectionLabel}>SLAs</div>
                        <div style={{ display: "flex", gap: 12 }}>
                          {detail.slas.filter(s => s.property).map((s, i) => (
                            <div key={i} style={styles.slaCard}>
                              <div style={{ fontSize: 20, fontWeight: 700, color: "#0f172a" }}>{s.value}</div>
                              <div style={{ fontSize: 11, color: "#64748b" }}>{s.unit}</div>
                              <div style={{ fontSize: 12, fontWeight: 600, color: "#334155", marginTop: 4 }}>{s.property}</div>
                            </div>
                          ))}
                        </div>
                      </div>
                    )}
                    {!detail.terms_usage && !detail.terms_limitations && (!detail.slas || detail.slas.length === 0) && (
                      <div style={{ color: "#94a3b8", fontSize: 13 }}>No terms or SLAs defined.</div>
                    )}
                  </div>
                )}
              </div>
            </div>
          )}
        </div>
      ))}

      <MarketplaceChatPanel
        open={chatOpen}
        onClose={() => setChatOpen(false)}
        initialDomain={detail?.domain || undefined}
        initialContractId={detail?.contract_id || undefined}
      />
    </div>
  );
}

function Section({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div style={styles.sectionLabel}>{label}</div>
      <div style={{ fontSize: 14, color: "#334155", lineHeight: 1.5 }}>{value}</div>
    </div>
  );
}

function StatCard({ label, value, color }: { label: string; value: string; color: string }) {
  return (
    <div style={{ padding: 14, borderRadius: 8, backgroundColor: "#fff", border: "1px solid #e2e8f0", position: "relative", overflow: "hidden" }}>
      <div style={{ position: "absolute", top: 0, left: 0, right: 0, height: 3, backgroundColor: color }} />
      <div style={{ fontSize: 24, fontWeight: 700, color: "#0f172a" }}>{value}</div>
      <div style={{ fontSize: 12, color: "#64748b" }}>{label}</div>
    </div>
  );
}

// OSI stat card on the marketplace Overview tab. Score-only — consumers
// see a band + completeness pill + a "conformance fail" hint, but no
// drill-into-analysis affordance. The PO sees the full breakdown via
// the My Products dashboard's View Analysis modal.
function OsiStatCard({
  band,
  completeness,
  conformancePass,
  rubricShortLabel,
}: {
  band: OsiBand;
  completeness: number | null;
  conformancePass: boolean | null;
  rubricShortLabel?: string | null;
}) {
  const palette = (band === "green"
    ? { bar: "#16a34a", value: "#16a34a", bg: "#fff" }
    : band === "amber"
    ? { bar: "#d97706", value: "#d97706", bg: "#fff" }
    : band === "red"
    ? { bar: "#dc2626", value: "#dc2626", bg: "#fff" }
    : { bar: "#cbd5e1", value: "#94a3b8", bg: "#fff" });
  const label = (rubricShortLabel || "OSI").trim() || "OSI";
  return (
    <div style={{ padding: 14, borderRadius: 8, backgroundColor: palette.bg, border: "1px solid #e2e8f0", position: "relative", overflow: "hidden" }}>
      <div style={{ position: "absolute", top: 0, left: 0, right: 0, height: 3, backgroundColor: palette.bar }} />
      <div style={{ fontSize: 24, fontWeight: 700, color: palette.value }}>
        {typeof completeness === "number" ? `${completeness}%` : "—"}
      </div>
      <div style={{ fontSize: 12, color: "#64748b", display: "flex", alignItems: "center", gap: 6 }}>
        <span>{label} readiness</span>
        {conformancePass === false && (
          <span title="Conformance fail" style={{ color: "#dc2626" }}>· conformance fail</span>
        )}
      </div>
    </div>
  );
}

const crossRefRowStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  padding: "8px 12px",
  borderRadius: 6,
  border: "1px solid #e2e8f0",
  backgroundColor: "#fff",
  fontSize: 13,
  cursor: "pointer",
  textAlign: "left",
};

type KindFilter = "all" | "source" | "aggregate" | "consumer";

function KindFilterChips({
  filter,
  onChange,
  counts,
}: {
  filter: KindFilter;
  onChange: (f: KindFilter) => void;
  counts: Record<KindFilter, number>;
}) {
  const chips: Array<{ key: KindFilter; label: string; activeBg: string; activeFg: string; activeBorder: string }> = [
    { key: "all",       label: "All",       activeBg: "#0f172a", activeFg: "#fff",    activeBorder: "#0f172a" },
    { key: "source",    label: "Source",    activeBg: "#dbeafe", activeFg: "#1d4ed8", activeBorder: "#93c5fd" },
    { key: "aggregate", label: "Aggregate", activeBg: "#ede9fe", activeFg: "#6d28d9", activeBorder: "#c4b5fd" },
    { key: "consumer",  label: "Consumer",  activeBg: "#fde4d4", activeFg: "#b86a1d", activeBorder: "#fdba74" },
  ];
  return (
    <div style={{ display: "flex", gap: 6, marginBottom: 4, flexWrap: "wrap" }}>
      {chips.map((c) => {
        const active = filter === c.key;
        return (
          <button
            key={c.key}
            type="button"
            onClick={() => onChange(c.key)}
            style={{
              padding: "4px 10px",
              borderRadius: 999,
              fontSize: 12,
              fontWeight: 600,
              cursor: "pointer",
              backgroundColor: active ? c.activeBg : "#fff",
              color: active ? c.activeFg : "#475569",
              border: `1px solid ${active ? c.activeBorder : "#cbd5e1"}`,
              whiteSpace: "nowrap",
            }}
          >
            {c.label} <span style={{ opacity: 0.7, fontWeight: 500 }}>({counts[c.key]})</span>
          </button>
        );
      })}
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  emptyState: { textAlign: "center", padding: "60px 24px", backgroundColor: "#fff", borderRadius: 12, border: "1px solid #e2e8f0" },
  card: { padding: 18, borderRadius: 10, border: "2px solid #e2e8f0", cursor: "pointer", transition: "all 0.15s" },
  cardHeader: { display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 },
  cardName: { fontWeight: 700, fontSize: 15, color: "#0f172a" },
  publishedBadge: { fontSize: 11, fontWeight: 600, padding: "2px 8px", borderRadius: 10, backgroundColor: "#dcfce7", color: "#16a34a" },
  cardDesc: { fontSize: 13, color: "#475569", lineHeight: 1.4, marginBottom: 10 },
  cardMeta: { display: "flex", gap: 12, fontSize: 12, color: "#94a3b8" },
  detailPanel: { backgroundColor: "#fff", borderRadius: 10, border: "1px solid #e2e8f0", overflow: "hidden" },
  detailHeader: { padding: "16px 20px", borderBottom: "1px solid #e2e8f0", display: "flex", justifyContent: "space-between", alignItems: "center" },
  closeBtn: { background: "none", border: "none", cursor: "pointer", fontSize: 18, color: "#94a3b8", lineHeight: 1 },
  deployBtn: { padding: "6px 14px", borderRadius: 6, border: "none", backgroundColor: "#16a34a", color: "#fff", fontWeight: 600, fontSize: 13, cursor: "pointer" },
  reportBtn: { padding: "6px 14px", borderRadius: 6, border: "1px solid #cbd5e1", backgroundColor: "#f8fafc", color: "#1e293b", fontWeight: 600, fontSize: 13, cursor: "pointer" },
  copyUrlBtn: { padding: "6px 10px", borderRadius: 6, border: "1px solid #cbd5e1", backgroundColor: "#ffffff", color: "#475569", fontWeight: 500, fontSize: 12, cursor: "pointer" },
  editInFlightBadge: { fontSize: 11, fontWeight: 600, padding: "2px 8px", borderRadius: 10, backgroundColor: "#fef3c7", color: "#92400e" },
  detailTabBar: { display: "flex", gap: 0, borderBottom: "2px solid #e2e8f0", alignItems: "stretch" },
  detailTabDivider: { width: 1, alignSelf: "center", height: 20, margin: "0 10px", backgroundColor: "#cbd5e1", flexShrink: 0 },
  detailTab: { padding: "10px 16px", border: "none", borderBottom: "2px solid transparent", marginBottom: -2, backgroundColor: "transparent", fontSize: 13, cursor: "pointer" },
  // Fill the viewport rather than a fixed 500px: a hard cap left long tab
  // content (e.g. a 13-column schema) looking truncated — the inner scrollbar
  // was easy to miss and users read the cut-off as missing rows.
  detailContent: { padding: 20, maxHeight: "calc(100vh - 230px)", minHeight: 300, overflow: "auto" },
  ownerBlock: { display: "flex", gap: 12, alignItems: "center", padding: 12, borderRadius: 8, border: "1px solid #e2e8f0" },
  ownerAvatar: { width: 36, height: 36, borderRadius: 18, backgroundColor: "#3b82f6", color: "#fff", display: "flex", alignItems: "center", justifyContent: "center", fontWeight: 700, fontSize: 14 },
  externalCatalog: { display: "flex", flexDirection: "column" as const, gap: 4, padding: 14, borderRadius: 8, border: "1px dashed #cbd5e1", backgroundColor: "#f8fafc" },
  tagChip: { fontSize: 11, fontWeight: 600, padding: "2px 8px", borderRadius: 10, backgroundColor: "#f1f5f9", color: "#475569", textTransform: "lowercase" as const },
  catalogBtn: { marginTop: 6, padding: "6px 14px", borderRadius: 5, border: "1px solid #cbd5e1", backgroundColor: "#e2e8f0", color: "#94a3b8", fontSize: 12, fontWeight: 600, cursor: "not-allowed", alignSelf: "flex-start" as const },
  table: { width: "100%", borderCollapse: "collapse" as const, fontSize: 13 },
  th: { textAlign: "left" as const, padding: "8px 12px", borderBottom: "2px solid #e2e8f0", color: "#64748b", fontWeight: 600, fontSize: 11, textTransform: "uppercase" as const },
  td: { padding: "6px 12px", borderBottom: "1px solid #f1f5f9", color: "#334155" },
  pkBadge: { fontSize: 10, fontWeight: 700, padding: "1px 6px", borderRadius: 4, backgroundColor: "#fef3c7", color: "#92400e" },
  qualityCard: { padding: 12, borderRadius: 8, border: "1px solid #e2e8f0", backgroundColor: "#f8fafc" },
  severityBadge: { fontSize: 10, fontWeight: 700, padding: "2px 6px", borderRadius: 4, color: "#fff" },
  sectionLabel: { fontSize: 12, fontWeight: 700, color: "#64748b", textTransform: "uppercase" as const, letterSpacing: "0.05em", marginBottom: 4 },
  slaCard: { padding: 14, borderRadius: 8, border: "1px solid #e2e8f0", backgroundColor: "#fff", textAlign: "center" as const, minWidth: 100 },
  lineageRow: { padding: "8px 12px", borderRadius: 6, border: "1px solid #e2e8f0", display: "flex", justifyContent: "space-between", alignItems: "center" },
  lineageViz: { display: "flex", alignItems: "center", justifyContent: "center", gap: 16, padding: 20, backgroundColor: "#f8fafc", borderRadius: 8, border: "1px solid #e2e8f0" },
  lineageCol: { display: "flex", flexDirection: "column" as const, gap: 6, alignItems: "center" },
  lineageLabel: { fontSize: 11, fontWeight: 700, color: "#64748b", textTransform: "uppercase" as const, marginBottom: 4 },
  lineageNode: { padding: "8px 14px", borderRadius: 6, border: "1px solid #e2e8f0", backgroundColor: "#fff", fontSize: 12, fontWeight: 600, textAlign: "center" as const },
  lineageNodeEmpty: { padding: "8px 14px", fontSize: 12, color: "#94a3b8" },
  lineageArrow: { fontSize: 16, color: "#94a3b8", fontFamily: "monospace" },
};

// ── Quality tab: grouped + filtered ────────────────────────────────────────
// Shares categorisation logic with the engineering project's DQ Rules card so
// users see the same buckets in both surfaces. Marketplace rules can also be
// contract-level (no per-column anchor) — those land under a "(contract-wide)"
// pseudo-table at the top.

type QualityRule = ProductDetail["quality_rules"][number];

const SOURCE_BADGE: Record<string, { bg: string; fg: string; label: string }> = {
  contract: { bg: "#e0f2fe", fg: "#075985", label: "Contract" },
  domain:   { bg: "#ede9fe", fg: "#5b21b6", label: "Domain" },
  user:     { bg: "#fef3c7", fg: "#92400e", label: "User" },
  spec:     { bg: "#dcfce7", fg: "#166534", label: "Spec" },
};

function severityColor(sev: string): string {
  const s = (sev || "").toLowerCase();
  if (s === "error" || s === "violation" || s === "sh:violation") return "#ef4444";
  if (s === "warning" || s === "sh:warning") return "#f59e0b";
  return "#3b82f6";
}

function MarketplaceQualityTab({ rules }: { rules: QualityRule[] }) {
  const [filterSeverity, setFilterSeverity] = useState("");
  const [filterSource, setFilterSource] = useState("");
  const [filterCategory, setFilterCategory] = useState<RuleCategory | "">("");
  const [collapsedTables, setCollapsedTables] = useState<Set<string>>(new Set());

  const valid = useMemo(() => rules.filter((r) => r.rule), [rules]);

  const sourceOptions = useMemo(() => {
    const set = new Set<string>();
    for (const r of valid) if (r.source) set.add(r.source);
    return Array.from(set).sort();
  }, [valid]);

  const grouped = useMemo(() => {
    const byTable = new Map<string, Map<RuleCategory, QualityRule[]>>();
    for (const r of valid) {
      const sevLower = (r.severity || "").toLowerCase();
      if (filterSeverity && !sevLower.includes(filterSeverity.toLowerCase())) continue;
      if (filterSource && (r.source || "") !== filterSource) continue;
      const cat = categorizeRuleType(r.rule);
      if (filterCategory && cat !== filterCategory) continue;
      const table = r.dataset_name || (r.column_name ? "(unanchored)" : "(contract-wide)");
      let cats = byTable.get(table);
      if (!cats) {
        cats = new Map();
        byTable.set(table, cats);
      }
      const list = cats.get(cat) || [];
      list.push(r);
      cats.set(cat, list);
    }
    return byTable;
  }, [valid, filterSeverity, filterSource, filterCategory]);

  if (valid.length === 0) {
    return <div style={{ color: "#94a3b8", fontSize: 13 }}>No quality rules defined.</div>;
  }

  const toggleTable = (t: string) => {
    const next = new Set(collapsedTables);
    if (next.has(t)) next.delete(t);
    else next.add(t);
    setCollapsedTables(next);
  };

  // Pin pseudo-tables to the top so anchored datasets sort alphabetically below.
  const PIN = ["(contract-wide)", "(unanchored)"];
  const sortedTables = Array.from(grouped.keys()).sort((a, b) => {
    const ai = PIN.indexOf(a);
    const bi = PIN.indexOf(b);
    if (ai >= 0 && bi >= 0) return ai - bi;
    if (ai >= 0) return -1;
    if (bi >= 0) return 1;
    return a.localeCompare(b);
  });

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
        <select
          value={filterSeverity}
          onChange={(e) => setFilterSeverity(e.target.value)}
          style={qualityFilterStyle}
        >
          <option value="">All severities</option>
          <option value="error">Error / Violation</option>
          <option value="warning">Warning</option>
          <option value="info">Info</option>
        </select>
        {sourceOptions.length > 0 && (
          <select
            value={filterSource}
            onChange={(e) => setFilterSource(e.target.value)}
            style={qualityFilterStyle}
          >
            <option value="">All sources</option>
            {sourceOptions.map((s) => (
              <option key={s} value={s}>
                {SOURCE_BADGE[s]?.label || s}
              </option>
            ))}
          </select>
        )}
        <select
          value={filterCategory}
          onChange={(e) => setFilterCategory(e.target.value as RuleCategory | "")}
          style={qualityFilterStyle}
        >
          <option value="">All categories</option>
          {RULE_CATEGORIES.map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
        {(filterSeverity || filterSource || filterCategory) && (
          <button
            onClick={() => { setFilterSeverity(""); setFilterSource(""); setFilterCategory(""); }}
            style={{ padding: "5px 12px", borderRadius: 5, border: "1px solid #cbd5e1", backgroundColor: "#fff", color: "#64748b", fontWeight: 600, fontSize: 12, cursor: "pointer" }}
          >
            Clear
          </button>
        )}
      </div>

      {grouped.size === 0 ? (
        <div style={{ color: "#94a3b8", fontSize: 13 }}>No rules match the current filters.</div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {sortedTables.map((table) => {
            const cats = grouped.get(table)!;
            const total = Array.from(cats.values()).reduce((n, l) => n + l.length, 0);
            const collapsed = collapsedTables.has(table);
            return (
              <div key={table} style={{ border: "1px solid #e2e8f0", borderRadius: 8, backgroundColor: "#fff" }}>
                <button
                  type="button"
                  onClick={() => toggleTable(table)}
                  style={{
                    width: "100%",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    padding: "10px 14px",
                    background: "none",
                    border: "none",
                    borderBottom: collapsed ? "none" : "1px solid #f1f5f9",
                    cursor: "pointer",
                    fontSize: 14,
                    fontWeight: 700,
                    color: "#0f172a",
                    textAlign: "left",
                  }}
                >
                  <span>
                    <span style={{ marginRight: 8, color: "#94a3b8", fontSize: 12 }}>
                      {collapsed ? "▶" : "▼"}
                    </span>
                    {table}
                  </span>
                  <span style={{ fontSize: 12, color: "#64748b", fontWeight: 500 }}>
                    {total} rule{total === 1 ? "" : "s"}
                  </span>
                </button>
                {!collapsed && (
                  <div style={{ padding: "4px 0" }}>
                    {RULE_CATEGORIES.map((cat) => {
                      const list = cats.get(cat);
                      if (!list || list.length === 0) return null;
                      const color = CATEGORY_COLORS[cat];
                      return (
                        <div key={cat} style={{ padding: "8px 14px" }}>
                          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
                            <span
                              style={{
                                fontSize: 11,
                                fontWeight: 700,
                                letterSpacing: 0.5,
                                color: color.fg,
                                backgroundColor: color.bg,
                                padding: "2px 8px",
                                borderRadius: 10,
                              }}
                            >
                              {cat.toUpperCase()}
                            </span>
                            <span style={{ fontSize: 11, color: "#94a3b8" }}>{list.length}</span>
                          </div>
                          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                            {list.map((q, i) => {
                              const src = (q.source || "contract").toLowerCase();
                              const badge = SOURCE_BADGE[src] || SOURCE_BADGE.contract;
                              return (
                                <div key={i} style={styles.qualityCard}>
                                  <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 4, flexWrap: "wrap" }}>
                                    <span style={{ ...styles.severityBadge, backgroundColor: severityColor(q.severity) }}>
                                      {q.severity || "info"}
                                    </span>
                                    <span style={{ fontWeight: 600, fontSize: 13 }}>{q.name || q.rule}</span>
                                    {q.column_name && (
                                      <span style={{ fontSize: 11, color: "#64748b", fontFamily: "monospace" }}>
                                        {q.column_name}
                                      </span>
                                    )}
                                    {q.dimension && <span style={{ fontSize: 11, color: "#64748b" }}>{q.dimension}</span>}
                                    <span
                                      style={{
                                        fontSize: 10,
                                        fontWeight: 600,
                                        letterSpacing: 0.3,
                                        color: badge.fg,
                                        backgroundColor: badge.bg,
                                        padding: "1px 6px",
                                        borderRadius: 4,
                                        marginLeft: "auto",
                                      }}
                                      title={`Source: ${src}`}
                                    >
                                      {badge.label}
                                    </span>
                                  </div>
                                  {q.description && <div style={{ fontSize: 13, color: "#334155" }}>{q.description}</div>}
                                  {q.businessImpact && <div style={{ fontSize: 12, color: "#64748b", marginTop: 4, fontStyle: "italic" }}>{q.businessImpact}</div>}
                                </div>
                              );
                            })}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

const qualityFilterStyle: React.CSSProperties = {
  padding: "5px 8px",
  borderRadius: 5,
  border: "1px solid #cbd5e1",
  fontSize: 12,
  backgroundColor: "#fff",
  color: "#0f172a",
  minWidth: 130,
};
