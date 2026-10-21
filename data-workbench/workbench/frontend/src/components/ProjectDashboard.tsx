import { Fragment, useEffect, useMemo, useState } from "react";
import api from "../api/client";
import QualityScorePanel from "./QualityScorePanel";
import QuestionsPanel from "./QuestionsPanel";
import DQResultsPanel from "./DQResultsPanel";
import {
  categorizeRuleType,
  RULE_CATEGORIES,
  CATEGORY_COLORS,
  type RuleCategory,
} from "../lib/ruleCategory";
import type { UnmappedColumn } from "../types";
import MappingGraphView from "./MappingGraphView";
import DatasetERDView from "./DatasetERDView";
import ColumnNeighborhoodView from "./ColumnNeighborhoodView";
import ProductKindChip from "./ProductKindChip";
import { downloadBlobZip } from "../lib/download";

// serving_mode → the package download endpoint suffix.
const SERVING_MODE_PACKAGE: Record<string, string> = {
  virtual_view: "view-package",
  dbt_materialized: "dbt-project",
  lakehouse_local: "lakehouse-package",
};
import RelationshipKindChip from "./RelationshipKindChip";
import SensitivityChip from "./SensitivityChip";
import ServingWarningsCallout from "./ServingWarningsCallout";
import JoinsOverridePanel from "./JoinsOverridePanel";
import FilterReviewPanel from "./FilterReviewPanel";
import PreviewTab from "./PreviewTab";
import DeploymentReflectionPanel from "./DeploymentReflectionPanel";
import ObjectStoreServingRow from "./ObjectStoreServingRow";
import { servingModeLabel, servingModeColor } from "../lib/servingMode";

interface SummaryStats {
  // Project's archetype — drives which cards we show. dpe-cf is consumer-
  // aligned post-split; dpe-sa is source-aligned; dq/dd/legacy keep the
  // full card set.
  archetype?: string;
  datasets: number;
  columns: number;
  graph_nodes: number;
  graph_relationships: number;
  descriptions_total: number;
  descriptions_approved: number;
  descriptions_rejected: number;
  descriptions_pending: number;
  final_descriptions: number;
  // PO-approved :RelationshipDescription nodes on the project's FK
  // edges. Always present in the response; populated only for
  // post-multi-project (scoped) projects.
  relationships_total: number;
  relationships_approved: number;
  relationships_rejected: number;
  relationships_pending: number;
  mappings_total: number;
  mappings_approved: number;
  mappings_rejected: number;
  mappings_pending: number;
  serving_definitions: number;
  profiled_columns: number;
  profiling_metrics: number;
  dq_rules: number;
  dq_rules_observed: number;
  dq_rules_domain: number;
  dq_rules_user: number;
  dq_rules_spec: number;
  dq_rules_pending: number;
  dq_rules_approved: number;
  dq_rules_rejected: number;
  // Latest DQ test-run rollup (from SQLite; null/0 until a run exists).
  dq_tests_last_run_at: string | null;
  dq_tests_framework: string | null;
  dq_tests_status: string | null;
  dq_tests_total: number;
  dq_tests_passed: number;
  dq_tests_failed: number;
  dq_tests_tables: number;
  dq_tests_run_count: number;
  allowed_values: number;
  playbook_items: number;
  learning_cycles: number;
  quality_composite: number | null;
  scored_datasets: number;
  data_products: number;
  dprod_columns_total: number;
  dprod_columns_mapped: number;
  dprod_columns_unmapped: number;
  // Consumer-aligned only — counts of CONSUMES'd source products.
  inputs_source_products?: number;
  inputs_datasets?: number;
  inputs_columns?: number;
}

// Cards that don't make sense on a consumer-aligned project — its sources
// are already-published source products, not raw discovery artifacts. The
// equivalent context lives in the marketplace detail of each consumed
// source product.
const CONSUMER_HIDDEN_CARDS = new Set([
  "datasets",
  "columns",
  "descriptions",
  "final_descriptions",
  "profiling",
  // NOTE: dq_rules is intentionally NOT hidden — consumer products DO carry DQ
  // rules on their contract (:DProdColumn spec/domain/user). The dq_rules card +
  // its detail query branch to the dprod variant for dpe-cf (summary.py), so the
  // card shows the product's contract rules rather than an empty :Column set.
  "allowed_values",
  "quality_score",
  // Consumer projects read FK relationship semantics via :CONSUMES from
  // the upstream source products; they don't author their own
  // :RelationshipDescription nodes.
  "relationships",
]);

interface FilterOptions {
  tables: string[];
  columns: string[];
  severities: string[];
}

interface CardDef {
  key: string;
  label: string;
  value: (s: SummaryStats) => string;
  subtitle: (s: SummaryStats) => string;
  color: string;
  detailCard: string | null;
  filters: ("table" | "column" | "severity" | "source")[];
  // Optional visibility predicate — the card is hidden until it returns true.
  // Used by the DQ results card, which only appears once a test run exists.
  show?: (s: SummaryStats) => boolean;
}

const CARDS: CardDef[] = [
  {
    key: "datasets", label: "Datasets",
    value: (s) => String(s.datasets),
    subtitle: () => "Tables discovered",
    color: "#3b82f6", detailCard: "datasets",
    filters: ["table"],
  },
  {
    key: "columns", label: "Columns",
    value: (s) => String(s.columns),
    subtitle: () => "Across all tables",
    color: "#6366f1", detailCard: "columns",
    filters: ["table", "column"],
  },
  {
    key: "graph", label: "Graph Nodes",
    value: (s) => String(s.graph_nodes),
    subtitle: (s) => `${s.graph_relationships} relationships`,
    color: "#8b5cf6", detailCard: null,
    filters: [],
  },
  {
    key: "descriptions", label: "Descriptions",
    value: (s) => String(s.descriptions_total),
    subtitle: (s) => {
      const p: string[] = [];
      if (s.descriptions_approved) p.push(`${s.descriptions_approved} approved`);
      if (s.descriptions_rejected) p.push(`${s.descriptions_rejected} rejected`);
      if (s.descriptions_pending) p.push(`${s.descriptions_pending} pending`);
      return p.join(" / ") || "None generated";
    },
    color: "#22c55e", detailCard: "descriptions",
    filters: ["table", "column"],
  },
  {
    key: "final_descriptions", label: "Final Descriptions",
    value: (s) => String(s.final_descriptions),
    subtitle: () => "Current active descriptions",
    color: "#14b8a6", detailCard: "final_descriptions",
    filters: ["table", "column"],
  },
  {
    // PO-reviewed FK relationship semantics. Surfaced on dpe-sa
    // projects after metadata_enrichment writes the descriptions and
    // before the PO source-validation gate clears them. The detail
    // card lets engineers see the descriptions as a read-only view;
    // editing happens in the PO validation panel's Relationships tab.
    key: "relationships", label: "Relationships",
    value: (s) => String(s.relationships_total),
    subtitle: (s) => {
      const p: string[] = [];
      if (s.relationships_approved) p.push(`${s.relationships_approved} approved`);
      if (s.relationships_rejected) p.push(`${s.relationships_rejected} rejected`);
      if (s.relationships_pending) p.push(`${s.relationships_pending} pending`);
      return p.join(" / ") || "None generated yet";
    },
    color: "#a855f7", detailCard: "relationships",
    filters: ["table"],
  },
  {
    // Renamed from "Data Products" — the headline number is the column
    // count of the project's product, which "Product Columns" reads as.
    // The detail view (data_products card) still walks DProdColumn rows.
    key: "data_products", label: "Product Columns",
    value: (s) => String(s.dprod_columns_total),
    subtitle: (s) => {
      if (s.data_products === 0) return "No product defined";
      const mapped = s.dprod_columns_mapped;
      const total = s.dprod_columns_total;
      if (total === 0) return "No columns yet";
      const pct = Math.round((mapped / total) * 100);
      return `${mapped}/${total} mapped · ${pct}%`;
    },
    color: "#7c3aed", detailCard: "data_products",
    filters: ["table", "column"],
  },
  {
    // Consumer-aligned only. CARD_FOR_ARCHETYPE filters this out for SA
    // and legacy archetypes (which don't have :CONSUMES).
    key: "inputs", label: "Inputs",
    value: (s) => String(s.inputs_source_products ?? 0),
    subtitle: (s) => {
      const products = s.inputs_source_products ?? 0;
      if (products === 0) return "No source products consumed";
      const datasets = s.inputs_datasets ?? 0;
      const columns = s.inputs_columns ?? 0;
      return `${datasets} dataset${datasets === 1 ? "" : "s"} · ${columns} columns`;
    },
    color: "#0ea5e9", detailCard: "inputs",
    filters: [],
  },
  {
    key: "mappings", label: "Mappings",
    value: (s) => {
      // When a product exists, the headline number is "mapped/total" — that
      // matches the user's mental model ("how much of my product is wired up").
      if (s.dprod_columns_total > 0) {
        return `${s.dprod_columns_mapped}/${s.dprod_columns_total}`;
      }
      return String(s.mappings_total);
    },
    subtitle: (s) => {
      if (s.dprod_columns_total > 0) {
        const pct = Math.round((s.dprod_columns_mapped / s.dprod_columns_total) * 100);
        const parts: string[] = [`${pct}% mapped`];
        if (s.dprod_columns_unmapped > 0) parts.push(`${s.dprod_columns_unmapped} unmapped`);
        if (s.mappings_pending) parts.push(`${s.mappings_pending} pending review`);
        return parts.join(" · ");
      }
      if (s.data_products === 0 && s.mappings_total === 0) return "Awaiting product";
      const p: string[] = [];
      if (s.mappings_approved) p.push(`${s.mappings_approved} approved`);
      if (s.mappings_rejected) p.push(`${s.mappings_rejected} rejected`);
      if (s.mappings_pending) p.push(`${s.mappings_pending} pending`);
      return p.join(" / ") || "None generated";
    },
    color: "#f59e0b", detailCard: "mappings",
    filters: ["table", "column"],
  },
  {
    key: "serving", label: "Data Serving",
    value: (s) => String(s.serving_definitions),
    subtitle: (s) => s.serving_definitions > 0 ? "Serving definition(s)" : "No serving configured",
    color: "#059669", detailCard: "serving",
    filters: [],
  },
  {
    key: "profiling", label: "Data Profiling",
    value: (s) => String(s.profiled_columns),
    subtitle: (s) => `${s.profiling_metrics} metrics collected`,
    color: "#7c3aed", detailCard: "profiling",
    filters: ["table", "column"],
  },
  {
    key: "dq_rules", label: "DQ Rules",
    value: (s) => String(s.dq_rules),
    subtitle: (s) => {
      const sources: string[] = [];
      if (s.dq_rules_observed) sources.push(`${s.dq_rules_observed} observed`);
      if (s.dq_rules_domain) sources.push(`${s.dq_rules_domain} domain`);
      if (s.dq_rules_user) sources.push(`${s.dq_rules_user} user`);
      if (s.dq_rules_spec) sources.push(`${s.dq_rules_spec} spec`);
      const states: string[] = [];
      if (s.dq_rules_pending) states.push(`${s.dq_rules_pending} pending`);
      if (s.dq_rules_rejected) states.push(`${s.dq_rules_rejected} rejected`);
      const left = sources.join(", ");
      const right = states.join(", ");
      if (!left && !right) return "Property shapes";
      if (!right) return left;
      if (!left) return right;
      return `${left} · ${right}`;
    },
    color: "#ef4444", detailCard: "dq_rules",
    filters: ["table", "column", "severity", "source"],
  },
  {
    // DQ test results — appears only once tests have actually run (keyed on
    // dq_tests_last_run_at). Headline is passed/total; click opens the run
    // history + per-expectation breakdown (custom panel, like quality_score).
    key: "dq_results", label: "Data Quality",
    value: (s) => `${s.dq_tests_passed}/${s.dq_tests_total}`,
    subtitle: (s) => {
      const parts: string[] = [];
      if (s.dq_tests_failed > 0) parts.push(`${s.dq_tests_failed} failed`);
      else if (s.dq_tests_total > 0) parts.push("all passed");
      if (s.dq_tests_tables) parts.push(`${s.dq_tests_tables} table${s.dq_tests_tables === 1 ? "" : "s"}`);
      if (s.dq_tests_framework) parts.push(s.dq_tests_framework.toUpperCase());
      return parts.join(" · ") || "Expectations passed";
    },
    color: "#0d9488",
    detailCard: "dq_results",
    filters: [],
    show: (s) => !!s.dq_tests_last_run_at,
  },
  {
    key: "allowed_values", label: "Allowed Values",
    value: (s) => String(s.allowed_values),
    subtitle: () => "Columns with >95% coverage",
    color: "#ec4899", detailCard: "allowed_values",
    filters: ["table", "column"],
  },
  {
    key: "quality_score", label: "Quality Score",
    value: (s) => s.quality_composite != null ? `${Math.round(s.quality_composite * 100)}%` : "\u2014",
    subtitle: (s) => s.scored_datasets > 0 ? `${s.scored_datasets} dataset(s) scored` : "Not yet scored",
    color: "#10b981", detailCard: "quality_scores",
    filters: [],
  },
  {
    key: "playbook", label: "Domain Playbook",
    value: (s) => String(s.playbook_items),
    subtitle: () => "Active playbook rules",
    color: "#0ea5e9", detailCard: "playbook",
    filters: [],
  },
  {
    key: "learning", label: "Learning History",
    value: (s) => String(s.learning_cycles),
    subtitle: (s) => s.learning_cycles === 0 ? "No reflection cycles yet" : `${s.learning_cycles} reflection cycle(s)`,
    color: "#a855f7", detailCard: "learning_history",
    filters: [],
  },
  {
    // Q&A — questions this data product can answer + gap analysis. The
    // card's headline number is intentionally static ("View") because the
    // count lives on the :QAEvaluation sidecar (fetched lazily when the
    // panel mounts). Custom render path below (like quality_score).
    key: "qa", label: "Q&A",
    value: () => "View",
    subtitle: () => "Questions this product can answer",
    color: "#0ea5e9", detailCard: "qa",
    filters: [],
  },
];

const DETAIL_COLUMNS: Record<string, { key: string; label: string; wide?: boolean }[]> = {
  datasets: [
    { key: "schema", label: "Schema" },
    { key: "name", label: "Table" },
    { key: "relationship_kind", label: "Kind" },
    { key: "column_count", label: "Columns" },
    { key: "description", label: "Description", wide: true },
  ],
  columns: [
    { key: "schema", label: "Schema" },
    { key: "table_name", label: "Table" },
    { key: "column_name", label: "Column" },
    { key: "data_type", label: "Data Type" },
  ],
  descriptions: [
    { key: "schema", label: "Schema" },
    { key: "table_name", label: "Table" },
    { key: "column_name", label: "Column" },
    { key: "description", label: "Description", wide: true },
    { key: "status", label: "Status" },
  ],
  final_descriptions: [
    { key: "schema", label: "Schema" },
    { key: "table_name", label: "Table" },
    { key: "column_name", label: "Column" },
    { key: "description", label: "Description", wide: true },
    { key: "status", label: "Status" },
  ],
  relationships: [
    { key: "table_name", label: "From Table" },
    { key: "to_table", label: "To Table" },
    { key: "relationship_nature", label: "Nature" },
    { key: "description", label: "Description", wide: true },
    { key: "status", label: "Status" },
  ],
  mappings: [
    { key: "product_column", label: "Product Column" },
    { key: "schema", label: "Schema" },
    { key: "table_name", label: "Table" },
    { key: "source_column", label: "Source Column" },
    { key: "lookup_tables", label: "Lookup" },
    { key: "transform_kind", label: "Transform" },
    { key: "transform_author", label: "By" },
    { key: "rationale", label: "Rationale", wide: true },
    { key: "score", label: "Score" },
    { key: "status", label: "Status" },
  ],
  data_products: [
    { key: "product_name", label: "Product" },
    { key: "dataset_name", label: "Dataset" },
    { key: "column_name", label: "Column" },
    { key: "physical_type", label: "Type" },
    { key: "primary_key", label: "PK" },
    { key: "sensitivity", label: "Sensitivity" },
    { key: "description", label: "Description", wide: true },
    { key: "mapping_status", label: "Mapping" },
  ],
  inputs: [
    { key: "source_product_name", label: "Source Product" },
    { key: "product_kind", label: "Kind" },
    { key: "dataset_count", label: "Datasets" },
    { key: "column_count", label: "Columns" },
  ],
  serving: [
    { key: "serving_mode", label: "Serving Mode" },
    { key: "view_name", label: "View Name" },
    { key: "dbt_materialization", label: "dbt" },
    { key: "target_schema", label: "Target Schema" },
    { key: "build_status", label: "Build" },
    { key: "deployment_status", label: "Deploy" },
    { key: "platform", label: "Platform" },
    { key: "ddl", label: "DDL", wide: true },
  ],
  profiling: [
    { key: "schema", label: "Schema" },
    { key: "table_name", label: "Table" },
    { key: "column_name", label: "Column" },
    { key: "metric", label: "Metric" },
    { key: "value", label: "Value" },
    { key: "unit", label: "Unit" },
  ],
  dq_rules: [
    { key: "table_name", label: "Table" },
    { key: "column_name", label: "Column" },
    { key: "rule_type", label: "Rule Type" },
    { key: "rule_source", label: "Source" },
    { key: "rule_status", label: "Status" },
    { key: "severity", label: "Severity" },
    { key: "description", label: "Description", wide: true },
  ],
  allowed_values: [
    { key: "schema", label: "Schema" },
    { key: "table_name", label: "Table" },
    { key: "column_name", label: "Column" },
    { key: "allowed_values", label: "Values", wide: true },
    { key: "value_count", label: "# Values" },
    { key: "coverage", label: "Coverage" },
  ],
  playbook: [
    { key: "phase", label: "Phase" },
    { key: "rule", label: "Rule", wide: true },
    { key: "version", label: "Version" },
    { key: "operation", label: "Operation" },
    { key: "rationale", label: "Rationale", wide: true },
    { key: "updated_at", label: "Updated" },
  ],
  learning_history: [
    { key: "version", label: "Version" },
    { key: "phase", label: "Phase" },
    { key: "summary", label: "Summary", wide: true },
    { key: "items_added", label: "Added" },
    { key: "items_updated", label: "Updated" },
    { key: "items_removed", label: "Removed" },
    { key: "created_at", label: "Date" },
  ],
  provenance: [
    { key: "activity_type", label: "Activity" },
    { key: "outcome", label: "Outcome" },
    { key: "occurred_at", label: "When" },
    { key: "agent", label: "Agent" },
    { key: "agent_type", label: "Type" },
    { key: "description", label: "Description / Rationale", wide: true },
    { key: "status", label: "Status" },
    { key: "rejection_category", label: "Rejection" },
  ],
};

interface Props {
  projectId: number;
  /**
   * Optional callback invoked when the user clicks "Create mapping…" on an
   * unmapped row. Parent (ProjectDetailPage) is expected to select the
   * data_mapping stage and switch to the Reviews tab so the engineer lands
   * on the UnmappedColumnsPanel pre-focused on this column.
   */
  onNavigateToUnmapped?: (columnUri: string) => void;
  /**
   * Optional callback invoked from the Mappings card's graph view to open
   * the data_mapping stage's Reviews tab (without targeting a specific
   * column). Used for "Open in Reviews →" deep-link from the embedded
   * graph preview.
   */
  onNavigateToMappingReview?: () => void;
  /** Bump to force a re-fetch of summary stats (e.g. after a stage completes). */
  refreshKey?: number;
}

interface ProductUsage {
  total_tokens: number;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  events: number;
  by_source?: { source: string; total_tokens: number }[];
}

/** Compact LLM-token strip for one data product (project), fed by the usage
 * ledger. Re-fetches when refreshKey bumps (e.g. after a stage completes). */
function ProductUsageStrip({ projectId, refreshKey }: { projectId: number; refreshKey?: number }) {
  const [u, setU] = useState<ProductUsage | null>(null);
  useEffect(() => {
    api.get(`/api/usage/by-project/${projectId}`).then((r) => setU(r.data)).catch(() => setU(null));
  }, [projectId, refreshKey]);
  if (!u || (u.total_tokens ?? 0) <= 0) return null;
  const fmt = (n: number) => (n ?? 0).toLocaleString();
  const top = (u.by_source || []).slice(0, 4).map((s) => `${s.source} ${fmt(s.total_tokens)}`).join(" · ");
  return (
    <div
      style={{
        display: "flex", alignItems: "baseline", gap: 16, flexWrap: "wrap",
        padding: "10px 14px", borderRadius: 10, backgroundColor: "#f8fafc",
        border: "1px solid #e2e8f0",
      }}
      title={"Working tokens (uncached input + output) for this product across all LLM activity." + (top ? `\n\nBy source: ${top}` : "")}
    >
      <span style={{ fontSize: 12, fontWeight: 700, color: "#334155", textTransform: "uppercase", letterSpacing: "0.04em" }}>
        LLM tokens
      </span>
      <span style={{ fontSize: 18, fontWeight: 700, color: "#0f172a" }}>{fmt(u.total_tokens)}</span>
      <span style={{ fontSize: 12, color: "#64748b" }}>
        {fmt(u.input_tokens)} in / {fmt(u.output_tokens)} out · ${ (u.cost_usd ?? 0).toFixed(2) } · {u.events} call{u.events === 1 ? "" : "s"}
      </span>
    </div>
  );
}

function ServingGitRow({ projectId }: { projectId: number }) {
  const [cfg, setCfg] = useState<{ configured: boolean; repo_url: string | null }>({
    configured: false, repo_url: null,
  });
  const [pushing, setPushing] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    api.get(`/api/projects/${projectId}/serving/git-status`)
      .then((r) => setCfg({ configured: !!r.data.configured, repo_url: r.data.repo_url || null }))
      .catch(() => {});
  }, [projectId]);
  if (!cfg.configured && !cfg.repo_url) return null;
  const push = async () => {
    setPushing(true);
    setErr(null);
    try {
      const r = await api.post(`/api/projects/${projectId}/serving/push-to-git`);
      setCfg((p) => ({ ...p, repo_url: r.data.repo_url || p.repo_url }));
    } catch (e) {
      const d = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setErr(d || "Push failed");
    }
    setPushing(false);
  };
  return (
    <div style={{ marginTop: 16, paddingTop: 12, borderTop: "1px solid #e2e8f0" }}>
      <div style={{ fontSize: 11, fontWeight: 700, color: "#475569", textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: 6 }}>
        Git repository
      </div>
      <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
        {cfg.repo_url ? (
          <a href={cfg.repo_url} target="_blank" rel="noreferrer" style={{ fontSize: 13, fontWeight: 600, color: "#7c3aed", textDecoration: "none" }}>
            {cfg.repo_url} →
          </a>
        ) : (
          <span style={{ fontSize: 13, color: "#94a3b8" }}>Not pushed yet.</span>
        )}
        {cfg.configured && (
          <button
            type="button"
            onClick={push}
            disabled={pushing}
            style={{ fontSize: 12, padding: "4px 10px", border: "1px solid #cbd5e1", borderRadius: 5,
              background: "#fff", color: "#7c3aed", cursor: pushing ? "default" : "pointer", fontWeight: 600 }}
          >
            {pushing ? "Pushing…" : cfg.repo_url ? "Re-push ↑" : "Push to Git ↑"}
          </button>
        )}
        {err && <span style={{ fontSize: 12, color: "#ef4444" }}>{err}</span>}
      </div>
    </div>
  );
}

export default function ProjectDashboard({ projectId, onNavigateToUnmapped, onNavigateToMappingReview, refreshKey }: Props) {
  const [stats, setStats] = useState<SummaryStats | null>(null);
  const [qaCount, setQaCount] = useState<number | null>(null);
  const [activeCard, setActiveCard] = useState<string | null>(null);
  const [detailRows, setDetailRows] = useState<Record<string, unknown>[]>([]);
  // Expanded transform-detail rows in the Mappings drill-down (view the full DSL
  // after the fact, from Project Summary — not just at the mapping-review stage).
  const [expandedMappingRows, setExpandedMappingRows] = useState<Set<number>>(new Set());
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailCypher, setDetailCypher] = useState("");

  // Filters
  const [filterOptions, setFilterOptions] = useState<FilterOptions>({ tables: [], columns: [], severities: [] });
  const [filterTable, setFilterTable] = useState("");
  const [filterColumn, setFilterColumn] = useState("");
  const [filterSeverity, setFilterSeverity] = useState("");
  const [filterSource, setFilterSource] = useState("");
  // Category is client-side only — the backend doesn't bucket rule types
  // and the bucket boundaries are a UI concept.
  const [filterCategory, setFilterCategory] = useState<RuleCategory | "">("");
  const [servingPreviewOpen, setServingPreviewOpen] = useState(false);
  const [collapsedTables, setCollapsedTables] = useState<Set<string>>(new Set());

  // Provenance
  const [provenanceColUri, setProvenanceColUri] = useState<string | null>(null);
  const [provenanceColName, setProvenanceColName] = useState("");
  const [provenanceRows, setProvenanceRows] = useState<Record<string, unknown>[]>([]);
  const [provenanceLoading, setProvenanceLoading] = useState(false);
  const [provenanceCypher, setProvenanceCypher] = useState("");

  // Collapse expanded transform-detail rows whenever a card's detail reloads
  // (the reset fires in loadDetail — kept out of an effect to avoid churn).
  const toggleMappingExpand = (idx: number) => {
    setExpandedMappingRows((prev) => {
      const next = new Set(prev);
      if (next.has(idx)) next.delete(idx);
      else next.add(idx);
      return next;
    });
  };

  // Mappings card has three views: the existing mapped table, a list of
  // :DProdColumn nodes with no current :ColumnMapping, or an embedded
  // graph preview. Reset to "mapped" whenever the active card changes so
  // the toggle doesn't leak.
  const [mappingsView, setMappingsView] = useState<"mapped" | "unmapped" | "graph">("mapped");
  const [unmappedRows, setUnmappedRows] = useState<UnmappedColumn[]>([]);
  const [unmappedLoading, setUnmappedLoading] = useState(false);

  // Datasets card: toggle between the list table and the ERD graph.
  const [datasetsView, setDatasetsView] = useState<"list" | "graph">("list");
  // Columns card: clicking a row drills into the neighborhood graph for
  // that column. The list ↔ neighborhood transition is single-column-scoped.
  const [columnsView, setColumnsView] = useState<"list" | "neighborhood">("list");
  const [selectedColumnUri, setSelectedColumnUri] = useState<string | null>(null);
  const [selectedColumnLabel, setSelectedColumnLabel] = useState<string>("");

  // Reset the sub-views whenever the user closes the card or jumps elsewhere.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (activeCard !== "mappings") setMappingsView("mapped");
    if (activeCard !== "datasets") setDatasetsView("list");
    if (activeCard !== "columns") {
      setColumnsView("list");
      setSelectedColumnUri(null);
    }
  }, [activeCard]);

  // Phase 3 drift map: per-source-uri lookup populated on first inputs-card
  // open. Used to render a "v1 → v2 available" chip per row when an upstream
  // source has advanced past the consumer's pinned consumedVersion.
  const [driftBySourceUri, setDriftBySourceUri] = useState<Record<string, { consumed: number; latest: number; kind: string | null }>>({});
  useEffect(() => {
    if (activeCard !== "inputs") return;
    let cancelled = false;
    api
      .get(`/api/projects/${projectId}/upstream-drift`)
      .then((r) => {
        if (cancelled) return;
        const map: Record<string, { consumed: number; latest: number; kind: string | null }> = {};
        for (const s of (r.data.sources || []) as Array<{
          source_dprod_uri: string;
          drifted: boolean;
          consumed_version: number;
          source_current_version: number;
          classifier?: { kind: string };
        }>) {
          if (s.drifted) {
            map[s.source_dprod_uri] = {
              consumed: s.consumed_version,
              latest: s.source_current_version,
              kind: s.classifier?.kind ?? null,
            };
          }
        }
        setDriftBySourceUri(map);
      })
      .catch(() => {
        if (!cancelled) setDriftBySourceUri({});
      });
    return () => {
      cancelled = true;
    };
  }, [activeCard, projectId]);

  useEffect(() => {
    api.get(`/api/projects/${projectId}/summary`).then((r) => setStats(r.data)).catch(() => {});
    api.get(`/api/projects/${projectId}/summary/filters`).then((r) => setFilterOptions(r.data)).catch(() => {});
    // Q&A question count for the card (0 when none generated yet).
    api.get(`/api/projects/${projectId}/qa/latest`)
      .then((r) => setQaCount(Array.isArray(r.data?.questions) ? r.data.questions.length : 0))
      .catch(() => setQaCount(0));
  }, [projectId, refreshKey]);

  const loadUnmappedColumns = async () => {
    setUnmappedLoading(true);
    try {
      const res = await api.get(`/api/projects/${projectId}/reviews/unmapped_columns`);
      setUnmappedRows((res.data.items as UnmappedColumn[]) ?? []);
    } catch {
      setUnmappedRows([]);
    }
    setUnmappedLoading(false);
  };

  const loadDetail = async (cardKey: string, table = "", column = "", severity = "", source = "") => {
    setDetailLoading(true);
    setProvenanceColUri(null);
    setExpandedMappingRows(new Set());
    const params: Record<string, string> = { card: cardKey };
    if (table) params.table = table;
    if (column) params.column = column;
    if (severity) params.severity = severity;
    if (source) params.source = source;
    try {
      const res = await api.get(`/api/projects/${projectId}/summary/detail`, { params });
      setDetailRows(res.data.rows);
      setDetailCypher(res.data.cypher || "");
    } catch {
      setDetailRows([]);
      setDetailCypher("");
    }
    setDetailLoading(false);
  };

  const handleCardClick = (card: CardDef) => {
    if (!card.detailCard) return;
    if (activeCard === card.key) {
      setActiveCard(null);
      return;
    }
    setActiveCard(card.key);
    setFilterTable("");
    setFilterColumn("");
    setFilterSeverity("");
    setFilterSource("");
    setFilterCategory("");
    setCollapsedTables(new Set());
    // Quality score, Q&A, and DQ results use their own panels, not the generic
    // Cypher-backed detail table (DQ results come from SQLite via /test-runs).
    if (card.key !== "quality_score" && card.key !== "qa" && card.key !== "dq_results") {
      loadDetail(card.detailCard);
    }
  };

  const handleFilter = () => {
    if (!activeCard) return;
    const cardDef = CARDS.find((c) => c.key === activeCard);
    if (!cardDef?.detailCard) return;
    loadDetail(cardDef.detailCard, filterTable, filterColumn, filterSeverity, filterSource);
  };

  const handleProvenanceClick = async (params: { col_uri?: string; mapping_uri?: string }, label: string) => {
    setProvenanceColUri(label); // reuse as general provenance label
    setProvenanceColName(label);
    setProvenanceLoading(true);
    try {
      const res = await api.get(`/api/projects/${projectId}/summary/provenance`, { params });
      setProvenanceRows(res.data.rows);
      setProvenanceCypher(res.data.cypher || "");
    } catch {
      setProvenanceRows([]);
      setProvenanceCypher("");
    }
    setProvenanceLoading(false);
  };

  if (!stats) return <div style={{ color: "#94a3b8" }}>Loading summary...</div>;

  const activeCardDef = CARDS.find((c) => c.key === activeCard);
  const columns = activeCard ? DETAIL_COLUMNS[activeCard] || [] : [];
  const hasFilters = activeCardDef && activeCardDef.filters.length > 0;

  // Filter the card set by archetype:
  //   dpe-cf (consumer-aligned) → hide raw-graph cards (datasets/columns/etc.)
  //                               since the project doesn't run discovery.
  //                               The Inputs card is always included.
  //   anything else            → hide the Inputs card (no :CONSUMES edges).
  const isConsumer = stats.archetype === "dpe-cf";
  const visibleCards = CARDS.filter((card) => {
    if (isConsumer && CONSUMER_HIDDEN_CARDS.has(card.key)) return false;
    if (!isConsumer && card.key === "inputs") return false;
    if (card.show && !card.show(stats)) return false;
    return true;
  });

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <ProductUsageStrip projectId={projectId} refreshKey={refreshKey} />
      {/* Stat Cards */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(170px, 1fr))", gap: 12 }}>
        {visibleCards.map((card) => {
          const isActive = activeCard === card.key;
          const hasDetail = card.detailCard !== null;
          return (
            <div
              key={card.key}
              onClick={() => handleCardClick(card)}
              style={{
                padding: "16px 18px", borderRadius: 10, backgroundColor: "#fff",
                border: isActive ? `2px solid ${card.color}` : "1px solid #e2e8f0",
                cursor: hasDetail ? "pointer" : "default",
                transition: "all 0.15s", position: "relative", overflow: "hidden",
              }}
            >
              <div style={{ position: "absolute", top: 0, left: 0, right: 0, height: 3, backgroundColor: card.color }} />
              <div style={{ fontSize: 28, fontWeight: 700, color: "#0f172a", lineHeight: 1.1, marginBottom: 4 }}>
                {card.key === "qa" ? (qaCount ?? 0) : card.value(stats)}
              </div>
              <div style={{ fontSize: 13, fontWeight: 600, color: "#334155", marginBottom: 2 }}>{card.label}</div>
              <div style={{ fontSize: 11, color: "#94a3b8" }}>{card.subtitle(stats)}</div>
            </div>
          );
        })}
      </div>

      {/* Quality Score Panel (custom rendering) */}
      {activeCard === "quality_score" && (
        <div style={{ backgroundColor: "#fff", borderRadius: 10, border: "1px solid #e2e8f0", overflow: "hidden" }}>
          <div style={{ padding: "12px 18px", borderBottom: "1px solid #e2e8f0", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <span style={{ fontWeight: 600, fontSize: 14, color: "#334155" }}>Data Quality Scores</span>
            <button onClick={() => setActiveCard(null)} style={{ background: "none", border: "none", cursor: "pointer", fontSize: 18, color: "#94a3b8", lineHeight: 1 }}>x</button>
          </div>
          <div style={{ padding: "16px 18px" }}>
            <QualityScorePanel projectId={projectId} />
          </div>
        </div>
      )}

      {/* Q&A Panel — questions the product can answer + free-form probe.
          Fetches the latest :QAEvaluation on mount; the engineer can re-run
          analysis on demand. Custom render path mirrors quality_score. */}
      {activeCard === "qa" && (
        <div style={{ backgroundColor: "#fff", borderRadius: 10, border: "1px solid #e2e8f0", overflow: "hidden" }}>
          <div style={{ padding: "12px 18px", borderBottom: "1px solid #e2e8f0", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <span style={{ fontWeight: 600, fontSize: 14, color: "#334155" }}>Q&amp;A — what this product can answer</span>
            <button onClick={() => setActiveCard(null)} style={{ background: "none", border: "none", cursor: "pointer", fontSize: 18, color: "#94a3b8", lineHeight: 1 }}>x</button>
          </div>
          <div style={{ padding: "16px 18px" }}>
            <QuestionsPanel projectId={projectId} canRegenerate canProbe />
          </div>
        </div>
      )}

      {/* DQ Results Panel — run history + latest-run per-expectation breakdown.
          Custom render path (SQLite-backed via /test-runs), mirrors quality_score. */}
      {activeCard === "dq_results" && (
        <div style={{ backgroundColor: "#fff", borderRadius: 10, border: "1px solid #e2e8f0", overflow: "hidden" }}>
          <div style={{ padding: "12px 18px", borderBottom: "1px solid #e2e8f0", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <span style={{ fontWeight: 600, fontSize: 14, color: "#334155" }}>Data Quality — test results</span>
            <button onClick={() => setActiveCard(null)} style={{ background: "none", border: "none", cursor: "pointer", fontSize: 18, color: "#94a3b8", lineHeight: 1 }}>x</button>
          </div>
          <div style={{ padding: "16px 18px" }}>
            <DQResultsPanel projectId={projectId} />
          </div>
        </div>
      )}

      {/* Detail Table */}
      {activeCard && activeCard !== "quality_score" && activeCard !== "qa" && activeCard !== "dq_results" && (
        <div style={{ backgroundColor: "#fff", borderRadius: 10, border: "1px solid #e2e8f0", overflow: "hidden" }}>
          {/* Header */}
          <div style={{ padding: "12px 18px", borderBottom: "1px solid #e2e8f0", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <span style={{ fontWeight: 600, fontSize: 14, color: "#334155" }}>
              {activeCardDef?.label} Detail
              {!detailLoading && activeCard !== "mappings" && (
                <span style={{ fontWeight: 400, color: "#94a3b8", marginLeft: 8 }}>({detailRows.length} rows)</span>
              )}
            </span>
            <span style={{ display: "flex", alignItems: "center", gap: 10 }}>
              {activeCard === "serving" && (() => {
                // Offer a download for each distinct serving mode present.
                const suffixes = Array.from(new Set(
                  detailRows
                    .map((r) => SERVING_MODE_PACKAGE[String((r as Record<string, unknown>).serving_mode || "")])
                    .filter(Boolean) as string[],
                ));
                return suffixes.map((suffix) => (
                  <button
                    key={suffix}
                    onClick={() => void downloadBlobZip(
                      `/api/projects/${projectId}/serving/${suffix}?format=zip`,
                      `${projectId}-${suffix}.zip`,
                    ).catch(() => {})}
                    title="Download the runnable serving package"
                    style={{
                      padding: "3px 9px", borderRadius: 6, border: "1px solid #cbd5e1",
                      background: "#fff", color: "#475569", fontSize: 12, fontWeight: 600,
                      cursor: "pointer",
                    }}
                  >
                    ⤓ {suffix.replace("-package", "").replace("-project", "")}
                  </button>
                ));
              })()}
              <button onClick={() => setActiveCard(null)} style={{ background: "none", border: "none", cursor: "pointer", fontSize: 18, color: "#94a3b8", lineHeight: 1 }}>x</button>
            </span>
          </div>

          {/* Mapped / Unmapped / Graph toggle (only for the Mappings card) */}
          {activeCard === "mappings" && (
            <div style={{ padding: "10px 18px", borderBottom: "1px solid #e2e8f0", display: "flex", gap: 8, alignItems: "center" }}>
              {(["mapped", "unmapped", "graph"] as const).map((v) => {
                const count = v === "mapped"
                  ? stats.dprod_columns_mapped
                  : v === "unmapped"
                  ? stats.dprod_columns_unmapped
                  : null;
                const isActive = mappingsView === v;
                return (
                  <button
                    key={v}
                    onClick={() => {
                      setMappingsView(v);
                      if (v === "unmapped" && unmappedRows.length === 0) {
                        loadUnmappedColumns();
                      }
                    }}
                    style={{
                      padding: "5px 14px", borderRadius: 5,
                      border: isActive ? "none" : "1px solid #cbd5e1",
                      backgroundColor: isActive ? "#f59e0b" : "#fff",
                      color: isActive ? "#fff" : "#475569",
                      fontWeight: 600, fontSize: 12, cursor: "pointer",
                      textTransform: "capitalize",
                    }}
                  >
                    {v}{count !== null ? ` (${count})` : ""}
                  </button>
                );
              })}
              <span style={{ marginLeft: "auto", fontSize: 11, color: "#94a3b8" }}>
                {mappingsView === "mapped"
                  ? "Product columns with a current :ColumnMapping"
                  : mappingsView === "unmapped"
                  ? "Product columns the data_mapping skill could not auto-resolve"
                  : "Source-to-product topology — click Open in Reviews to edit"}
              </span>
            </div>
          )}

          {/* List / Graph toggle (only for the Datasets card) */}
          {activeCard === "datasets" && (
            <div style={{ padding: "10px 18px", borderBottom: "1px solid #e2e8f0", display: "flex", gap: 8, alignItems: "center" }}>
              {(["list", "graph"] as const).map((v) => {
                const isActive = datasetsView === v;
                return (
                  <button
                    key={v}
                    onClick={() => setDatasetsView(v)}
                    style={{
                      padding: "5px 14px", borderRadius: 5,
                      border: isActive ? "none" : "1px solid #cbd5e1",
                      backgroundColor: isActive ? "#3b82f6" : "#fff",
                      color: isActive ? "#fff" : "#475569",
                      fontWeight: 600, fontSize: 12, cursor: "pointer",
                      textTransform: "capitalize",
                    }}
                  >
                    {v === "graph" ? "ERD" : v}
                  </button>
                );
              })}
              <span style={{ marginLeft: "auto", fontSize: 11, color: "#94a3b8" }}>
                {datasetsView === "list"
                  ? "Tables discovered in the source schema"
                  : "Tables and foreign-key relationships — drag nodes to rearrange"}
              </span>
            </div>
          )}

          {/* Back-to-list when viewing a column's neighborhood */}
          {activeCard === "columns" && columnsView === "neighborhood" && (
            <div style={{ padding: "10px 18px", borderBottom: "1px solid #e2e8f0", display: "flex", gap: 8, alignItems: "center" }}>
              <button
                onClick={() => { setColumnsView("list"); setSelectedColumnUri(null); }}
                style={{
                  padding: "5px 14px", borderRadius: 5,
                  border: "1px solid #cbd5e1", backgroundColor: "#fff",
                  color: "#475569", fontWeight: 600, fontSize: 12, cursor: "pointer",
                }}
              >
                ← Back to columns list
              </button>
              <span style={{ marginLeft: 12, fontSize: 12, color: "#0f172a", fontWeight: 600 }}>
                {selectedColumnLabel}
              </span>
              <span style={{ marginLeft: "auto", fontSize: 11, color: "#94a3b8" }}>
                Lineage, rules, and recent test results for this column
              </span>
            </div>
          )}

          {/* Filters (hidden in mappings unmapped/graph views, the datasets ERD, and the columns neighborhood — they don't filter) */}
          {hasFilters
            && !(activeCard === "mappings" && (mappingsView === "unmapped" || mappingsView === "graph"))
            && !(activeCard === "datasets" && datasetsView === "graph")
            && !(activeCard === "columns" && columnsView === "neighborhood")
            && (
            <div style={{ padding: "10px 18px", borderBottom: "1px solid #e2e8f0", display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
              {activeCardDef!.filters.includes("table") && (
                <select value={filterTable} onChange={(e) => setFilterTable(e.target.value)} style={filterSelectStyle}>
                  <option value="">All tables</option>
                  {filterOptions.tables.map((t) => <option key={t} value={t}>{t}</option>)}
                </select>
              )}
              {activeCardDef!.filters.includes("column") && (
                <select value={filterColumn} onChange={(e) => setFilterColumn(e.target.value)} style={filterSelectStyle}>
                  <option value="">All columns</option>
                  {filterOptions.columns.map((c) => <option key={c} value={c}>{c}</option>)}
                </select>
              )}
              {activeCardDef!.filters.includes("severity") && (
                <select value={filterSeverity} onChange={(e) => setFilterSeverity(e.target.value)} style={filterSelectStyle}>
                  <option value="">All severities</option>
                  {filterOptions.severities.map((s) => <option key={s} value={s}>{s}</option>)}
                </select>
              )}
              {activeCardDef!.filters.includes("source") && (
                <select value={filterSource} onChange={(e) => setFilterSource(e.target.value)} style={filterSelectStyle}>
                  <option value="">All sources</option>
                  <option value="observation">Observed</option>
                  <option value="domain">Domain</option>
                  <option value="user">User</option>
                  <option value="spec">Spec</option>
                </select>
              )}
              {activeCard === "dq_rules" && (
                <select
                  value={filterCategory}
                  onChange={(e) => setFilterCategory(e.target.value as RuleCategory | "")}
                  style={filterSelectStyle}
                >
                  <option value="">All categories</option>
                  {RULE_CATEGORIES.map((c) => <option key={c} value={c}>{c}</option>)}
                </select>
              )}
              <button onClick={handleFilter} style={{ padding: "5px 14px", borderRadius: 5, border: "none", backgroundColor: "#3b82f6", color: "#fff", fontWeight: 600, fontSize: 12, cursor: "pointer" }}>
                Filter
              </button>
              {(filterTable || filterColumn || filterSeverity || filterSource || filterCategory) && (
                <button
                  onClick={() => { setFilterTable(""); setFilterColumn(""); setFilterSeverity(""); setFilterSource(""); setFilterCategory(""); loadDetail(activeCardDef!.detailCard!); }}
                  style={{ padding: "5px 14px", borderRadius: 5, border: "1px solid #cbd5e1", backgroundColor: "#fff", color: "#64748b", fontWeight: 600, fontSize: 12, cursor: "pointer" }}
                >
                  Clear
                </button>
              )}
            </div>
          )}

          {/* Cypher Query (hidden in mappings unmapped/graph views, the datasets ERD, and the columns neighborhood) */}
          {detailCypher && !detailLoading
            && !(activeCard === "mappings" && (mappingsView === "unmapped" || mappingsView === "graph"))
            && !(activeCard === "datasets" && datasetsView === "graph")
            && !(activeCard === "columns" && columnsView === "neighborhood")
            && <CypherBlock cypher={detailCypher} />}

          {/* Datasets ERD view — embedded DatasetERDView */}
          {activeCard === "datasets" && datasetsView === "graph" ? (
            <div style={{ padding: 12 }}>
              <DatasetERDView projectId={projectId} readOnly />
            </div>
          ) :
          /* Columns neighborhood view — embedded ColumnNeighborhoodView for the clicked column */
          activeCard === "columns" && columnsView === "neighborhood" && selectedColumnUri ? (
            <div style={{ padding: 12 }}>
              <ColumnNeighborhoodView projectId={projectId} colUri={selectedColumnUri} />
            </div>
          ) :
          /* Mappings graph view — embedded MappingGraphView with deep-link to Reviews */
          activeCard === "mappings" && mappingsView === "graph" ? (
            <div style={{ padding: 12 }}>
              {onNavigateToMappingReview && (
                <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 8 }}>
                  <button
                    onClick={onNavigateToMappingReview}
                    style={{
                      padding: "6px 14px",
                      borderRadius: 5,
                      border: "1px solid #cbd5e1",
                      backgroundColor: "#fff",
                      color: "#0f172a",
                      fontSize: 12,
                      fontWeight: 600,
                      cursor: "pointer",
                    }}
                  >
                    Open in Reviews →
                  </button>
                </div>
              )}
              <MappingGraphView projectId={projectId} readOnly />
            </div>
          ) : activeCard === "mappings" && mappingsView === "unmapped" ? (
            unmappedLoading ? (
              <div style={{ padding: 16, color: "#94a3b8" }}>Loading unmapped columns...</div>
            ) : unmappedRows.length === 0 ? (
              <div style={{ padding: 16, color: "#94a3b8" }}>
                Every product column in this project has a current mapping.
              </div>
            ) : (
              <div style={{ overflowX: "auto", maxHeight: 420 }}>
                <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
                  <thead>
                    <tr>
                      <th style={thStyle}>Product</th>
                      <th style={thStyle}>Dataset</th>
                      <th style={thStyle}>Product Column</th>
                      <th style={thStyle}>Type</th>
                      <th style={thStyle}>Description</th>
                      <th style={thStyle}>PO Hint</th>
                      {onNavigateToUnmapped && <th style={thStyle}>{""}</th>}
                    </tr>
                  </thead>
                  <tbody>
                    {unmappedRows.map((row, i) => (
                      <tr key={row.column_uri} style={{ backgroundColor: i % 2 === 0 ? "#fff" : "#f8fafc" }}>
                        <td style={tdStyle}>{row.product_name}</td>
                        <td style={tdStyle}>{row.dataset_name ?? "—"}</td>
                        <td style={{ ...tdStyle, fontWeight: 600 }}>{row.column_name}</td>
                        <td style={{ ...tdStyle, fontFamily: "monospace", fontSize: 12, color: "#64748b" }}>
                          {row.data_type ?? "—"}
                        </td>
                        <td style={{ ...tdStyle, maxWidth: 300, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={row.description ?? ""}>
                          {row.description ?? "—"}
                        </td>
                        <td style={tdStyle}>
                          {row.transform_hint?.kind ? (
                            <span style={{ fontSize: 10, fontWeight: 700, padding: "2px 6px", borderRadius: 4, backgroundColor: "#dbeafe", color: "#1e40af" }}>
                              {row.transform_hint.kind}
                              {row.transform_hint.inputs && row.transform_hint.inputs.length > 0 && (
                                <span style={{ fontWeight: 400, marginLeft: 4 }}>
                                  ({row.transform_hint.inputs.join(", ")})
                                </span>
                              )}
                            </span>
                          ) : (
                            <span style={{ fontSize: 11, color: "#cbd5e1" }}>—</span>
                          )}
                        </td>
                        {onNavigateToUnmapped && (
                          <td style={tdStyle}>
                            <button
                              onClick={() => onNavigateToUnmapped(row.column_uri)}
                              style={{
                                padding: "4px 10px", borderRadius: 4,
                                border: "1px solid #f59e0b", backgroundColor: "#fff",
                                color: "#b45309", fontSize: 11, fontWeight: 600,
                                cursor: "pointer", whiteSpace: "nowrap",
                              }}
                              title="Open the Unmapped Columns panel under the data_mapping stage's Reviews tab"
                            >
                              Create mapping…
                            </button>
                          </td>
                        )}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )
          ) : detailLoading ? (
            <div style={{ padding: 16, color: "#94a3b8" }}>Loading...</div>
          ) : detailRows.length === 0 ? (
            <div style={{ padding: 16, color: "#94a3b8" }}>No data available.</div>
          ) : activeCard === "dq_rules" ? (
            <DQRulesGroupedView
              rows={detailRows}
              filterCategory={filterCategory}
              collapsedTables={collapsedTables}
              setCollapsedTables={setCollapsedTables}
            />
          ) : (
            <div style={{ overflowX: "auto", maxHeight: 420 }}>
              {activeCard === "serving" && detailRows.length > 0 && (
                <div style={{ padding: "0 16px" }}>
                  <ServingWarningsCallout summaryJson={detailRows[0]?.summary_json as string | undefined} />
                  <ServingJoinsOverrideList
                    projectId={projectId}
                    summaryJson={detailRows[0]?.summary_json as string | undefined}
                  />
                  {(() => {
                    const status = (detailRows[0]?.deployment_status as string | undefined) || "pending";
                    const deployedAt = detailRows[0]?.deployed_at as string | undefined;
                    const deployedTo = detailRows[0]?.deployed_to as string | undefined;
                    const deploymentError = detailRows[0]?.deployment_error as string | undefined;
                    const deployed = status === "deployed";
                    return (
                      <div style={{ marginTop: 12, paddingTop: 12, borderTop: "1px solid #e2e8f0" }}>
                        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
                          <div style={{ fontSize: 12, color: "#475569" }}>
                            <strong>Deployment:</strong>{" "}
                            <span style={{
                              padding: "1px 6px", borderRadius: 4, fontWeight: 600,
                              backgroundColor: deployed ? "#dcfce7" : status === "failed" ? "#fee2e2" : "#f1f5f9",
                              color: deployed ? "#166534" : status === "failed" ? "#991b1b" : "#475569",
                            }}>
                              {status}
                            </span>
                            {deployed && deployedTo && (
                              <span style={{ marginLeft: 8, color: "#64748b" }}>
                                schema <code style={{ background: "#f1f5f9", padding: "1px 4px", borderRadius: 3 }}>{deployedTo}</code>
                              </span>
                            )}
                            {deployed && deployedAt && (
                              <span style={{ marginLeft: 8, color: "#94a3b8" }}>
                                {new Date(deployedAt).toLocaleString()}
                              </span>
                            )}
                            {status === "failed" && deploymentError && (
                              <span style={{ marginLeft: 8, color: "#991b1b" }}>
                                ({deploymentError})
                              </span>
                            )}
                          </div>
                          {deployed && (
                            <button
                              type="button"
                              onClick={() => setServingPreviewOpen(v => !v)}
                              style={{
                                fontSize: 12, padding: "4px 10px",
                                border: "1px solid #cbd5e1", borderRadius: 5,
                                backgroundColor: servingPreviewOpen ? "#f1f5f9" : "#fff",
                                color: "#0f172a", cursor: "pointer", fontWeight: 600,
                              }}
                            >
                              {servingPreviewOpen ? "Hide preview" : "Preview rows"}
                            </button>
                          )}
                        </div>
                        <ServingGitRow projectId={projectId} />
                        <ObjectStoreServingRow projectId={projectId} />
                        {deployed && servingPreviewOpen && (
                          <div style={{ marginTop: 12 }}>
                            <PreviewTab
                              endpoint={`/api/projects/${projectId}/serving/preview`}
                              datasets={[]}
                              limit={50}
                            />
                          </div>
                        )}
                        {deployed && (
                          <div style={{ marginTop: 16, paddingTop: 12, borderTop: "1px solid #e2e8f0" }}>
                            <div style={{ fontSize: 11, fontWeight: 700, color: "#475569", textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: 6 }}>
                              Deployment reflection
                            </div>
                            <DeploymentReflectionPanel projectId={projectId} />
                          </div>
                        )}
                      </div>
                    );
                  })()}
                </div>
              )}
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
                <thead>
                  <tr>
                    {columns.map((col) => <th key={col.key} style={thStyle}>{col.label}</th>)}
                    {(activeCard === "final_descriptions" || activeCard === "mappings") && <th style={thStyle}>Provenance</th>}
                    {activeCard === "columns" && <th style={thStyle}>Lineage</th>}
                    {activeCard === "inputs" && <th style={thStyle}>{""}</th>}
                  </tr>
                </thead>
                <tbody>
                  {detailRows.map((row, i) => (
                    <Fragment key={i}>
                    <tr style={{ backgroundColor: i % 2 === 0 ? "#fff" : "#f8fafc" }}>
                      {columns.map((col) => (
                        <td key={col.key} style={{ ...tdStyle, maxWidth: col.wide ? 300 : undefined, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: col.wide ? "nowrap" : undefined }} title={col.wide ? String(row[col.key] ?? "") : undefined}>
                          {activeCard === "mappings" && col.key === "transform_kind" ? (
                            <button
                              type="button"
                              onClick={() => toggleMappingExpand(i)}
                              title="Show the full transformation (inputs / params / expression)"
                              style={{ background: "none", border: "none", padding: 0, color: "#7c3aed", fontWeight: 600, cursor: "pointer", font: "inherit", display: "inline-flex", alignItems: "center", gap: 4 }}
                            >
                              <span aria-hidden>{expandedMappingRows.has(i) ? "▾" : "▸"}</span>
                              {renderCell(col.key, row[col.key])}
                            </button>
                          ) : (
                            renderCell(col.key, row[col.key])
                          )}
                        </td>
                      ))}
                      {activeCard === "final_descriptions" && (
                        <td style={tdStyle}>
                          <button
                            onClick={() => handleProvenanceClick({ col_uri: row.col_uri as string }, row.column_name as string)}
                            style={provBtnStyle}
                          >
                            View
                          </button>
                        </td>
                      )}
                      {activeCard === "mappings" && (
                        <td style={tdStyle}>
                          <button
                            onClick={() => handleProvenanceClick({ mapping_uri: row.mapping_uri as string }, `${row.source_column} → ${row.product_column}`)}
                            style={provBtnStyle}
                          >
                            View
                          </button>
                        </td>
                      )}
                      {activeCard === "columns" && (
                        <td style={tdStyle}>
                          {row.col_uri ? (
                            <button
                              onClick={() => {
                                setSelectedColumnUri(row.col_uri as string);
                                setSelectedColumnLabel(`${row.schema}.${row.table_name}.${row.column_name}`);
                                setColumnsView("neighborhood");
                              }}
                              style={provBtnStyle}
                              title="Show lineage, rules, and tests for this column"
                            >
                              Graph
                            </button>
                          ) : (
                            <span style={{ fontSize: 11, color: "#cbd5e1" }}>—</span>
                          )}
                        </td>
                      )}
                      {activeCard === "inputs" && (
                        <td style={tdStyle}>
                          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                            {row.source_product_uri ? (
                              <a
                                href={`/engineer/marketplace/${encodeURIComponent(String(row.source_product_uri))}`}
                                target="_blank"
                                rel="noopener noreferrer"
                                style={{
                                  ...provBtnStyle,
                                  textDecoration: "none",
                                  display: "inline-block",
                                }}
                                title="Open this source product in the marketplace (new tab)"
                              >
                                Open ↗
                              </a>
                            ) : (
                              <span style={{ fontSize: 11, color: "#cbd5e1" }}>—</span>
                            )}
                            {/* Phase 3 drift chip: rendered when this consumer's
                                consumedVersion lags the source's currentVersion. */}
                            {row.source_product_uri && driftBySourceUri[String(row.source_product_uri)] && (
                              <span
                                style={{
                                  padding: "2px 6px",
                                  fontSize: 10,
                                  fontWeight: 600,
                                  background:
                                    driftBySourceUri[String(row.source_product_uri)].kind === "breaking"
                                      ? "#fee2e2"
                                      : "#fef3c7",
                                  color:
                                    driftBySourceUri[String(row.source_product_uri)].kind === "breaking"
                                      ? "#991b1b"
                                      : "#92400e",
                                  borderRadius: 4,
                                }}
                                title={`Pinned v${driftBySourceUri[String(row.source_product_uri)].consumed} → live v${driftBySourceUri[String(row.source_product_uri)].latest}`}
                              >
                                v{driftBySourceUri[String(row.source_product_uri)].consumed} → v
                                {driftBySourceUri[String(row.source_product_uri)].latest}
                              </span>
                            )}
                          </div>
                        </td>
                      )}
                    </tr>
                    {activeCard === "mappings" && expandedMappingRows.has(i) && (
                      <tr>
                        <td
                          colSpan={columns.length + 1}
                          style={{ padding: 0, backgroundColor: "#faf5ff", borderBottom: "1px solid #e9d5ff" }}
                        >
                          {renderTransformDetail(row)}
                        </td>
                      </tr>
                    )}
                    </Fragment>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* Provenance Panel */}
      {provenanceColUri && (
        <div style={{ backgroundColor: "#fff", borderRadius: 10, border: "1px solid #e2e8f0", overflow: "hidden" }}>
          <div style={{ padding: "12px 18px", borderBottom: "1px solid #e2e8f0", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <span style={{ fontWeight: 600, fontSize: 14, color: "#334155" }}>Provenance: {provenanceColName}</span>
            <button onClick={() => setProvenanceColUri(null)} style={{ background: "none", border: "none", cursor: "pointer", fontSize: 18, color: "#94a3b8", lineHeight: 1 }}>x</button>
          </div>
          {provenanceCypher && !provenanceLoading && <CypherBlock cypher={provenanceCypher} />}
          {provenanceLoading ? (
            <div style={{ padding: 16, color: "#94a3b8" }}>Loading...</div>
          ) : provenanceRows.length === 0 ? (
            <div style={{ padding: 16, color: "#94a3b8" }}>No provenance records found.</div>
          ) : (
            <div style={{ overflowX: "auto", maxHeight: 300 }}>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
                <thead>
                  <tr>
                    {DETAIL_COLUMNS.provenance.map((col) => <th key={col.key} style={thStyle}>{col.label}</th>)}
                  </tr>
                </thead>
                <tbody>
                  {provenanceRows.map((row, i) => (
                    <tr key={i} style={{ backgroundColor: i % 2 === 0 ? "#fff" : "#f8fafc" }}>
                      {DETAIL_COLUMNS.provenance.map((col) => (
                        <td key={col.key} style={{ ...tdStyle, maxWidth: col.wide ? 250 : undefined, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: col.wide ? "nowrap" : undefined }} title={col.wide ? String(row[col.key] ?? "") : undefined}>
                          {renderCell(col.key, row[col.key])}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── Serving joins override list ────────────────────────────────────────────

/** Parses the per-view summaryJson and renders one collapsible
 *  JoinsOverridePanel per :DProdOutputDataset that the serving stage covered.
 *  Engineer authors explicit joins[] here when FK BFS picks a wrong bridge —
 *  the panel POSTs to /api/projects/{id}/dataset-transform/joins. */
function ServingJoinsOverrideList({
  projectId, summaryJson,
}: { projectId: number; summaryJson: string | undefined }) {
  if (!summaryJson) return null;
  let parsed: { views?: Array<{ view_name?: string; output_dataset_uri?: string }> } | null = null;
  try { parsed = JSON.parse(summaryJson); } catch { return null; }
  const views = parsed?.views || [];
  const renderable = views.filter((v) => !!v.output_dataset_uri);
  if (renderable.length === 0) return null;
  return (
    <>
      {renderable.map((v) => (
        <div key={v.output_dataset_uri}>
          <FilterReviewPanel
            projectId={projectId}
            outputDatasetUri={v.output_dataset_uri as string}
            viewName={v.view_name}
          />
          <JoinsOverridePanel
            projectId={projectId}
            outputDatasetUri={v.output_dataset_uri as string}
            viewName={v.view_name}
          />
        </div>
      ))}
    </>
  );
}

// ── Expandable DDL ────────────────────────────────────────────────────────

function ExpandableDDL({ ddl }: { ddl: string }) {
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState(false);

  return (
    <div>
      <button
        onClick={() => setOpen(!open)}
        style={{
          padding: "3px 10px", borderRadius: 4, border: "1px solid #059669",
          backgroundColor: open ? "#059669" : "#f0fdf4", color: open ? "#fff" : "#059669",
          fontSize: 11, fontWeight: 600, cursor: "pointer",
        }}
      >
        {open ? "Hide DDL" : "View DDL"}
      </button>
      {open && (
        <div style={{ marginTop: 6, position: "relative" }}>
          <button
            onClick={async () => { await navigator.clipboard.writeText(ddl); setCopied(true); setTimeout(() => setCopied(false), 1500); }}
            style={{
              position: "absolute", top: 6, right: 6, padding: "2px 8px", borderRadius: 4,
              border: "1px solid #334155", backgroundColor: copied ? "#22c55e" : "#1e293b",
              color: "#fff", fontSize: 10, fontWeight: 600, cursor: "pointer", zIndex: 1,
            }}
          >
            {copied ? "Copied" : "Copy"}
          </button>
          <pre style={{
            margin: 0, padding: "10px 14px", backgroundColor: "#0f172a", color: "#a5f3fc",
            borderRadius: 6, fontSize: 12, lineHeight: 1.5, overflow: "auto", maxHeight: 300,
            fontFamily: "'Fira Code', monospace", whiteSpace: "pre-wrap",
          }}>
            {ddl}
          </pre>
        </div>
      )}
    </div>
  );
}

// ── Cypher Block ───────────────────────────────────────────────────────────

function CypherBlock({ cypher }: { cypher: string }) {
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState(false);

  const handleCopy = async (e: React.MouseEvent) => {
    e.stopPropagation();
    await navigator.clipboard.writeText(cypher);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  return (
    <div style={{ borderBottom: "1px solid #e2e8f0" }}>
      <div
        onClick={() => setOpen(!open)}
        style={{
          padding: "6px 18px",
          display: "flex",
          alignItems: "center",
          gap: 8,
          cursor: "pointer",
          fontSize: 12,
          color: "#64748b",
          userSelect: "none",
        }}
      >
        <span style={{ fontSize: 10 }}>{open ? "\u25BC" : "\u25B6"}</span>
        <span style={{ fontWeight: 600 }}>Cypher Query</span>
        <button
          onClick={handleCopy}
          style={{
            marginLeft: "auto",
            padding: "2px 10px",
            borderRadius: 4,
            border: "1px solid #cbd5e1",
            backgroundColor: copied ? "#22c55e" : "#f8fafc",
            color: copied ? "#fff" : "#64748b",
            fontSize: 11,
            fontWeight: 600,
            cursor: "pointer",
            transition: "all 0.15s",
          }}
        >
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      {open && (
        <pre
          style={{
            margin: 0,
            padding: "10px 18px 14px",
            backgroundColor: "#0f172a",
            color: "#a5f3fc",
            fontSize: 12,
            lineHeight: 1.5,
            overflowX: "auto",
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
            fontFamily: "'Fira Code', 'Consolas', monospace",
          }}
        >
          {cypher}
        </pre>
      )}
    </div>
  );
}

// ── Styles ─────────────────────────────────────────────────────────────────

const thStyle: React.CSSProperties = {
  textAlign: "left", padding: "8px 14px", borderBottom: "2px solid #e2e8f0",
  color: "#64748b", fontWeight: 600, fontSize: 12, textTransform: "uppercase",
  letterSpacing: 0.5, position: "sticky", top: 0, backgroundColor: "#f8fafc",
};

const tdStyle: React.CSSProperties = {
  padding: "6px 14px", borderBottom: "1px solid #f1f5f9", color: "#334155",
};

const filterSelectStyle: React.CSSProperties = {
  padding: "5px 10px", borderRadius: 5, border: "1px solid #cbd5e1", fontSize: 12, backgroundColor: "#fff",
};

const provBtnStyle: React.CSSProperties = {
  padding: "2px 10px", borderRadius: 4, border: "1px solid #cbd5e1",
  backgroundColor: "#f8fafc", fontSize: 11, cursor: "pointer", fontWeight: 600, color: "#3b82f6",
};

// ── Cell renderer ──────────────────────────────────────────────────────────

const STATUS_COLORS: Record<string, string> = { approved: "#22c55e", rejected: "#ef4444", pending_review: "#8b5cf6" };
const SEVERITY_COLORS: Record<string, string> = { "sh:Violation": "#ef4444", "sh:Warning": "#f59e0b", "sh:Info": "#3b82f6" };

/** Read-only expansion of a mapping row's full transform DSL, shown inline in the
 *  Project-Summary Mappings drill-down so the engineer can inspect the derivation
 *  after the fact (kind / inputs / params / expression / decorators / lookup). */
function renderTransformDetail(row: Record<string, unknown>) {
  const kind = (row.transform_kind as string) || "direct";
  const expr = ((row.transform_expression as string) || "").trim();
  const parse = (v: unknown): unknown => {
    if (typeof v !== "string" || !v) return null;
    try { return JSON.parse(v); } catch { return null; }
  };
  const params = parse(row.transform_params) as Record<string, unknown> | null;
  const decorators = parse(row.transform_decorators) as Record<string, unknown> | null;
  const inputs = parse(row.transform_inputs) as string[] | null;
  const lookup = row.lookup_tables as string[] | undefined;
  const mono = {
    fontFamily: "monospace", fontSize: 11, backgroundColor: "#fff",
    padding: "1px 5px", borderRadius: 3, border: "1px solid #e9d5ff",
    wordBreak: "break-all" as const,
  };
  const hasParams = !!params && Object.keys(params).length > 0;
  const hasDeco = !!decorators && Object.keys(decorators).length > 0;
  return (
    <div style={{ padding: "8px 14px", fontSize: 12, color: "#334155", display: "flex", flexDirection: "column", gap: 4 }}>
      <div>
        <strong>Transform:</strong> <span style={mono}>{kind}</span>
        {inputs && inputs.length > 0 ? (
          <span style={{ color: "#94a3b8" }}> · {inputs.length} source column{inputs.length === 1 ? "" : "s"}</span>
        ) : null}
      </div>
      {hasParams ? <div><strong>Params:</strong> <span style={mono}>{JSON.stringify(params)}</span></div> : null}
      {expr ? <div><strong>Expression:</strong> <span style={mono}>{expr}</span></div> : null}
      {hasDeco ? <div><strong>Decorators:</strong> <span style={mono}>{JSON.stringify(decorators)}</span></div> : null}
      {lookup && lookup.length > 0 ? <div><strong>Lookup:</strong> {lookup.join(", ")}</div> : null}
      {!hasParams && !expr && !hasDeco && kind === "direct" ? (
        <div style={{ color: "#94a3b8" }}>Direct pass-through — no transformation.</div>
      ) : null}
    </div>
  );
}

function renderCell(key: string, val: unknown): React.ReactNode {
  if (val === null || val === undefined) return <span style={{ color: "#cbd5e1" }}>—</span>;

  // DDL fields render as expandable code blocks
  if (key === "ddl" && typeof val === "string") {
    return <ExpandableDDL ddl={val} />;
  }

  if (key === "serving_mode" && typeof val === "string") {
    return <span style={{ padding: "2px 8px", borderRadius: 10, fontSize: 11, fontWeight: 600, color: "#fff", backgroundColor: servingModeColor(val) }}>{servingModeLabel(val)}</span>;
  }

  if (key === "status" && typeof val === "string") {
    return <span style={{ padding: "2px 8px", borderRadius: 10, fontSize: 11, fontWeight: 600, color: "#fff", backgroundColor: STATUS_COLORS[val] || "#94a3b8" }}>{val.replace(/_/g, " ")}</span>;
  }
  if (key === "severity" && typeof val === "string") {
    return <span style={{ padding: "2px 8px", borderRadius: 10, fontSize: 11, fontWeight: 600, color: "#fff", backgroundColor: SEVERITY_COLORS[val] || "#94a3b8" }}>{val.replace("sh:", "")}</span>;
  }
  if (key === "outcome" && typeof val === "string") {
    const color = val === "approved" ? "#22c55e" : val === "rejected" ? "#ef4444" : "#94a3b8";
    return <span style={{ padding: "2px 8px", borderRadius: 10, fontSize: 11, fontWeight: 600, color: "#fff", backgroundColor: color }}>{val}</span>;
  }
  // Kind column on the Inputs detail card — render as a chip via the
  // shared component so the visual matches the marketplace and dashboards.
  if (key === "product_kind" && typeof val === "string") {
    return <ProductKindChip kind={val} compact />;
  }
  // Relationship-kind column on the Datasets detail card — same shared chip
  // used in marketplace + PO validation panel for visual consistency.
  if (key === "relationship_kind" && typeof val === "string") {
    return val ? <RelationshipKindChip kind={val} /> : <span style={{ color: "#cbd5e1" }}>—</span>;
  }
  // Sensitivity enum on column detail cards — chip self-hides for 'none'
  // so unflagged columns render as a quiet em-dash instead of nothing.
  if (key === "sensitivity" && typeof val === "string") {
    return val && val !== "none" ? <SensitivityChip sensitivity={val} compact /> : <span style={{ color: "#cbd5e1" }}>—</span>;
  }
  if (key === "allowed_values" && Array.isArray(val)) {
    return <span style={{ fontFamily: "monospace", fontSize: 12 }}>{val.join(", ")}</span>;
  }
  // Lookup reference tables a lookup-kind mapping reads from (often a different
  // CONSUMES'd product). The backend emits one entry per :LOOKUP_VIA edge, so
  // dedupe before rendering as small "lookup" chips.
  if (key === "lookup_tables" && Array.isArray(val)) {
    const tables = Array.from(new Set(val.filter((t): t is string => !!t)));
    if (tables.length === 0) return <span style={{ color: "#cbd5e1" }}>—</span>;
    return (
      <span style={{ display: "inline-flex", gap: 4, flexWrap: "wrap" }}>
        {tables.map((t) => (
          <span key={t} style={{ fontSize: 10, fontWeight: 700, color: "#9333ea", backgroundColor: "#f3e8ff", padding: "1px 6px", borderRadius: 3 }}>
            {t}
          </span>
        ))}
      </span>
    );
  }
  if (key === "score" && typeof val === "number") return val.toFixed(2);
  if (key === "coverage" && typeof val === "number") return `${(val * 100).toFixed(0)}%`;

  return String(val);
}

// ── DQ Rules grouped view ──────────────────────────────────────────────────
// Replaces the flat dq_rules table with a table → category accordion. Keeps
// all the renderCell semantics (severity / source / status pills) but drops
// them into per-(table,category) groups so a project with hundreds of rules
// remains scannable. Category bucketing is client-side via categorizeRuleType
// — the backend still ships a flat rowset, the grouping is a pure UI concern.

interface RuleRow {
  table_name?: string;
  column_name?: string;
  rule_type?: string;
  severity?: string;
  description?: string;
  rule_source?: string;
  rule_status?: string;
  [key: string]: unknown;
}

const SOURCE_BADGE: Record<string, { bg: string; fg: string; label: string }> = {
  observation: { bg: "#e0f2fe", fg: "#075985", label: "Observed" },
  domain:      { bg: "#ede9fe", fg: "#5b21b6", label: "Domain" },
  user:        { bg: "#fef3c7", fg: "#92400e", label: "User" },
  spec:        { bg: "#dcfce7", fg: "#166534", label: "Spec" },
};

function DQRulesGroupedView({
  rows,
  filterCategory,
  collapsedTables,
  setCollapsedTables,
}: {
  rows: RuleRow[];
  filterCategory: RuleCategory | "";
  collapsedTables: Set<string>;
  setCollapsedTables: (s: Set<string>) => void;
}) {
  const grouped = useMemo(() => {
    // Map<table, Map<category, RuleRow[]>>
    const byTable = new Map<string, Map<RuleCategory, RuleRow[]>>();
    for (const r of rows) {
      const cat = categorizeRuleType(r.rule_type);
      if (filterCategory && cat !== filterCategory) continue;
      const table = r.table_name || "(unknown table)";
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
  }, [rows, filterCategory]);

  if (grouped.size === 0) {
    return <div style={{ padding: 16, color: "#94a3b8" }}>No rules match the current filters.</div>;
  }

  const toggleTable = (table: string) => {
    const next = new Set(collapsedTables);
    if (next.has(table)) next.delete(table);
    else next.add(table);
    setCollapsedTables(next);
  };

  const sortedTables = Array.from(grouped.keys()).sort();

  return (
    <div style={{ overflowY: "auto", maxHeight: 500, display: "flex", flexDirection: "column", gap: 12 }}>
      {sortedTables.map((table) => {
        const cats = grouped.get(table)!;
        const total = Array.from(cats.values()).reduce((n, list) => n + list.length, 0);
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
                    <div key={cat} style={{ padding: "6px 14px" }}>
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
                      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                        {list.map((row, i) => {
                          const src = (row.rule_source || "observation").toLowerCase();
                          const badge = SOURCE_BADGE[src] || SOURCE_BADGE.observation;
                          return (
                            <div
                              key={i}
                              style={{
                                display: "grid",
                                gridTemplateColumns: "minmax(120px, 180px) minmax(110px, 140px) auto auto 1fr",
                                gap: 10,
                                fontSize: 12,
                                padding: "6px 8px",
                                borderRadius: 6,
                                backgroundColor: "#f8fafc",
                                alignItems: "center",
                              }}
                            >
                              <span style={{ color: "#0f172a", fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={row.column_name}>
                                {row.column_name}
                              </span>
                              <span style={{ color: "#475569", fontFamily: "monospace", fontSize: 11 }}>
                                {row.rule_type}
                              </span>
                              {renderCell("severity", row.severity)}
                              <span
                                style={{
                                  fontSize: 10,
                                  fontWeight: 600,
                                  letterSpacing: 0.3,
                                  color: badge.fg,
                                  backgroundColor: badge.bg,
                                  padding: "1px 6px",
                                  borderRadius: 4,
                                }}
                                title={`Source: ${src}`}
                              >
                                {badge.label}
                              </span>
                              <span style={{ color: "#334155", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={row.description}>
                                {row.description}
                              </span>
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
  );
}
