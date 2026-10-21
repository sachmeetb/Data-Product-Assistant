import { useCallback, useEffect, useState } from "react";
import api from "../api/client";
import type { StageInfo, ReviewType } from "../types";
import { canReview } from "../types";
import { useRole } from "../RoleContext";
import DescriptionReviewPanel from "./DescriptionReviewPanel";
import MappingReviewPanel from "./MappingReviewPanel";
import CodeSpecReviewPanel from "./CodeSpecReviewPanel";
import TransformationEscalationsPanel from "./TransformationEscalationsPanel";
import UnmappedColumnsPanel from "./UnmappedColumnsPanel";
import SourceProductValidationPanel from "./SourceProductValidationPanel";
import { useConfirm } from "./dialogContext";

interface Props {
  projectId: number;
  /**
   * Project archetype — drives review-surface filtering. For `dpe-sa` we
   * suppress the engineer-side `descriptions` panel so the PO is the only
   * approver (descriptions surface in `SourceProductValidationPanel`
   * instead). Engineer/steward UX is otherwise unchanged.
   */
  archetype?: string;
  stages: StageInfo[];
  onReviewComplete: () => void;
  /**
   * The active stage's `review_type`. Reviews are scoped per-stage so the PO
   * doesn't see noise from other stages. Cross-cutting types (unmapped_columns,
   * transformation_escalations) are anchored to data_mapping ("mappings") since
   * they originate from mapping work. When this is null/undefined the panel
   * renders an empty state — Reviews tab is meaningless without a stage context.
   */
  activeReviewType?: ReviewType | null;
  /**
   * Deep-link target from the project dashboard's "Create mapping…" affordance.
   * When set, the UnmappedColumnsPanel pre-expands the row whose column_uri
   * matches. The dashboard deep-link also sets activeReviewType="mappings"
   * via the parent's stage navigation, so the unmapped_columns section will
   * already be in the relevance set.
   */
  focusUnmappedColumnUri?: string | null;
  /** Called once the panel has consumed the focus signal so the parent can clear it. */
  onUnmappedFocusConsumed?: () => void;
  /**
   * data_mapping stage handle for MappingReviewPanel's "Re-run mapping"
   * affordance. Parent passes the matching StageInfo (or null if no
   * data_mapping stage exists), the workflow's repeatable flag, and a
   * helper that drives reset + WS run in the same code path as Pipeline.tsx.
   * Forwarded to MappingReviewPanel only; other sub-panels ignore them.
   */
  dataMappingStage?: StageInfo | null;
  dataMappingRepeatable?: boolean;
  onRerunStage?: (stageNumber: number, workflowId?: string) => Promise<void>;
  /**
   * "Guide me" affordance — MappingReviewPanel emits a column-scoped
   * question and we delegate to the parent to open the project chat
   * drawer with it pre-populated. Pure-frontend wiring; no backend
   * coupling beyond the existing chat session.
   */
  onGuideMe?: (prefill: string) => void;
  /**
   * Bumped by the parent when a stage completes (or on manual refresh). Drives a
   * refetch of the review counts + the mapping list, so a just-finished Data
   * Mapping shows its pending items without a manual page reload.
   */
  refreshKey?: number;
}

/** Per-stage relevance map: which review types belong on which stage's
 *  Reviews tab. Mapping-stage owns the cross-cutting unmapped/escalation
 *  surfaces because they originate from mapping work. */
const RELEVANT_REVIEW_TYPES: Record<ReviewType, readonly ReviewType[]> = {
  descriptions: ["descriptions"],
  mappings: ["mappings", "unmapped_columns", "transformation_escalations"],
  domain_rules: ["domain_rules"],
  // dpe-sa combined gate — its own dedicated panel renders all three
  // sub-buckets internally as tabs. Does not pull in other review types.
  source_product_validation: ["source_product_validation"],
  // The cross-cutting types don't have their own stages — they only render
  // via the parents above. These entries exist for type-completeness.
  unmapped_columns: ["unmapped_columns"],
  transformation_escalations: ["transformation_escalations"],
  // cmig code-migration spec review (its own stage, its own panel).
  code_spec: ["code_spec"],
};

const REVIEW_LABELS: Record<ReviewType, string> = {
  descriptions: "Description",
  mappings: "Mapping",
  domain_rules: "Domain Rule",
  transformation_escalations: "Transformation Escalation",
  unmapped_columns: "Unmapped Column",
  source_product_validation: "Source Product Validation",
  code_spec: "Code Spec",
};

const REVIEW_ROLE_HINTS: Record<ReviewType, string> = {
  descriptions: "Data Steward",
  mappings: "Reviewer",
  domain_rules: "Data Quality Analyst",
  transformation_escalations: "Data Steward",
  unmapped_columns: "Data Engineer",
  source_product_validation: "Data Product Owner",
  code_spec: "Data Engineer",
};

export default function ReviewPanel({
  projectId,
  archetype,
  stages,
  onReviewComplete,
  activeReviewType,
  focusUnmappedColumnUri,
  onUnmappedFocusConsumed,
  dataMappingStage,
  dataMappingRepeatable,
  onRerunStage,
  onGuideMe,
  refreshKey,
}: Props) {
  // dpe-sa: PO owns description approvals via SourceProductValidationPanel.
  // The engineer-side metadata_enrichment Reviews tab still flips to complete
  // automatically once the PO clears the queue (reviews._check_review_complete
  // walks all stages with the matching review_type), so suppressing the panel
  // here doesn't strand the stage.
  const suppressEngineerDescriptions = archetype === "dpe-sa";
  const role = useRole();
  const [pendingCounts, setPendingCounts] = useState<Record<ReviewType, number>>({
    descriptions: 0, mappings: 0, domain_rules: 0,
    transformation_escalations: 0, unmapped_columns: 0,
    source_product_validation: 0, code_spec: 0,
  });
  const [loading, setLoading] = useState(true);

  const loadCounts = useCallback(() => {
    return Promise.all([
      api.get(`/api/projects/${projectId}/reviews/descriptions`).then((r) => r.data.count).catch(() => 0),
      api.get(`/api/projects/${projectId}/reviews/mappings`).then((r) => r.data.count).catch(() => 0),
      api.get(`/api/projects/${projectId}/reviews/domain_rules`).then((r) => r.data.count).catch(() => 0),
      api.get(`/api/projects/${projectId}/reviews/transformation_escalations`).then((r) => r.data.count).catch(() => 0),
      api.get(`/api/projects/${projectId}/reviews/unmapped_columns`).then((r) => r.data.count).catch(() => 0),
      api.get(`/api/projects/${projectId}/reviews/source_product_validation`).then((r) => r.data.count).catch(() => 0),
    ]).then(([descCount, mapCount, ruleCount, escCount, unmappedCount, sourceValCount]) => {
      setPendingCounts({
        descriptions: descCount,
        mappings: mapCount,
        domain_rules: ruleCount,
        transformation_escalations: escCount,
        unmapped_columns: unmappedCount,
        source_product_validation: sourceValCount,
      });
    });
  }, [projectId]);

  useEffect(() => {
    setLoading(true);
    loadCounts().finally(() => setLoading(false));
    // refreshKey: re-pull counts when the parent signals a stage completed.
  }, [loadCounts, refreshKey]);

  // Refetch the badge counts after any review action (a mapping, approval,
  // etc.) so e.g. the "unmapped columns" count drops as soon as a column is
  // mapped — it used to stay stale until a full page reload. Also bubbles to
  // the parent's onReviewComplete (which refreshes the project/stages).
  const handleReviewComplete = useCallback(() => {
    loadCounts();
    onReviewComplete();
  }, [loadCounts, onReviewComplete]);

  const reviewStages = stages.filter((s) => s.status === "awaiting_review" && s.review_type);

  // Only consider review types that belong on this stage's Reviews tab. The
  // mapping stage owns the cross-cutting unmapped_columns / escalations
  // surfaces; every other stage shows just its own review_type.
  const relevant = new Set<ReviewType>(
    activeReviewType ? RELEVANT_REVIEW_TYPES[activeReviewType] : []
  );

  const reviewTypes = new Set<ReviewType>();
  for (const s of reviewStages) {
    if (
      (s.review_type === "descriptions"
        || s.review_type === "mappings"
        || s.review_type === "domain_rules"
        || s.review_type === "source_product_validation")
      && relevant.has(s.review_type)
    ) {
      reviewTypes.add(s.review_type);
    }
  }
  if (
    relevant.has("descriptions")
    && pendingCounts.descriptions > 0
    && !suppressEngineerDescriptions
  ) {
    reviewTypes.add("descriptions");
  }
  // Source-product validation always renders when on po_source_validation
  // — even when zero items pending, the panel shows an "all approved"
  // empty state per tab so the PO has confirmation.
  if (
    relevant.has("source_product_validation")
    && (activeReviewType === "source_product_validation" || pendingCounts.source_product_validation > 0)
  ) {
    reviewTypes.add("source_product_validation");
  }
  // Mappings panel always renders when on the data_mapping stage — even
  // after every pending mapping is approved — so the graph view stays
  // visible. MappingReviewPanel has its own empty-pending state that
  // shows the graph plus an "all reviewed" message.
  if (
    relevant.has("mappings")
    && (activeReviewType === "mappings" || pendingCounts.mappings > 0)
  ) {
    reviewTypes.add("mappings");
  }
  if (relevant.has("domain_rules") && pendingCounts.domain_rules > 0) reviewTypes.add("domain_rules");
  if (relevant.has("transformation_escalations") && pendingCounts.transformation_escalations > 0) {
    reviewTypes.add("transformation_escalations");
  }
  // Unmapped columns aren't a review per se but live on the mapping surface
  // so the engineer sees gaps left by the data_mapping skill. Force-include
  // when the dashboard deep-linked to a specific unmapped row (the dashboard
  // also targets the data_mapping stage so `relevant` already has it).
  if (relevant.has("unmapped_columns") && (pendingCounts.unmapped_columns > 0 || focusUnmappedColumnUri)) {
    reviewTypes.add("unmapped_columns");
  }
  // code_spec lives in SQLite, not Neo4j — surface it whenever its stage is the
  // active review (the panel itself shows approved vs awaiting state).
  if (relevant.has("code_spec") && activeReviewType === "code_spec") {
    reviewTypes.add("code_spec");
  }

  if (loading) {
    return <div style={{ color: "#64748b", fontSize: 14 }}>Checking for pending reviews...</div>;
  }

  if (!activeReviewType) {
    return <div style={{ color: "#64748b", fontSize: 14 }}>Select a stage to see its reviews.</div>;
  }

  if (reviewTypes.size === 0) {
    return <div style={{ color: "#64748b", fontSize: 14 }}>No pending reviews for this stage.</div>;
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 24 }}>
      {Array.from(reviewTypes).map((reviewType) => {
        const allowed = canReview(role, reviewType);

        if (!allowed) {
          const requiredRole = REVIEW_ROLE_HINTS[reviewType];
          return (
            <div
              key={reviewType}
              style={{
                padding: 20,
                backgroundColor: "#fff",
                borderRadius: 8,
                border: "1px solid #e2e8f0",
              }}
            >
              <div style={{ fontWeight: 600, marginBottom: 8, color: "#334155" }}>
                {REVIEW_LABELS[reviewType]} Review — {pendingCounts[reviewType]} pending
              </div>
              <div
                style={{
                  padding: "12px 16px",
                  backgroundColor: "#fef3c7",
                  borderRadius: 6,
                  borderLeft: "3px solid #f59e0b",
                  fontSize: 13,
                  color: "#92400e",
                }}
              >
                This review requires the <strong>{requiredRole}</strong> or{" "}
                <strong>Reviewer</strong> role. Switch your role using the selector
                in the header to perform this review.
              </div>
            </div>
          );
        }

        return (
          <div key={reviewType}>
            {reviewType === "descriptions" && (
              <DescriptionReviewPanel projectId={projectId} onReviewComplete={handleReviewComplete} />
            )}
            {reviewType === "code_spec" && (
              <CodeSpecReviewPanel projectId={projectId} onReviewComplete={handleReviewComplete} />
            )}
            {reviewType === "mappings" && (
              <MappingReviewPanel
                projectId={projectId}
                onReviewComplete={handleReviewComplete}
                dataMappingStage={dataMappingStage ?? null}
                dataMappingRepeatable={dataMappingRepeatable ?? false}
                onRerunStage={onRerunStage}
                onGuideMe={onGuideMe}
                refreshKey={refreshKey}
              />
            )}
            {reviewType === "domain_rules" && (
              <DomainRuleReviewPanel projectId={projectId} onReviewComplete={handleReviewComplete} />
            )}
            {reviewType === "transformation_escalations" && (
              <TransformationEscalationsPanel projectId={projectId} onReviewComplete={handleReviewComplete} />
            )}
            {reviewType === "unmapped_columns" && (
              <UnmappedColumnsPanel
                projectId={projectId}
                onReviewComplete={handleReviewComplete}
                focusColumnUri={focusUnmappedColumnUri ?? null}
                onFocusConsumed={onUnmappedFocusConsumed}
                onGuideMe={onGuideMe}
              />
            )}
            {reviewType === "source_product_validation" && (
              <SourceProductValidationPanel projectId={projectId} onReviewComplete={handleReviewComplete} />
            )}
          </div>
        );
      })}
    </div>
  );
}


// ── Inline Domain Rule Review Panel ────────────────────────────────────────

interface DomainRuleItem {
  schema: string;
  table_name: string;
  col_uri: string;
  col_name: string;
  data_type: string;
  rule_uri: string;
  rule_type: string;
  severity: string;
  description: string;
  confidence: number | null;
}

function DomainRuleReviewPanel({ projectId, onReviewComplete }: { projectId: number; onReviewComplete: () => void }) {
  const confirm = useConfirm();
  const [items, setItems] = useState<DomainRuleItem[]>([]);
  const [index, setIndex] = useState(0);
  const [loading, setLoading] = useState(true);
  const [stats, setStats] = useState({ approved: 0, rejected: 0 });

  const loadItems = async () => {
    setLoading(true);
    try {
      const res = await api.get(`/api/projects/${projectId}/reviews/domain_rules`);
      setItems(res.data.items || []);
      setIndex(0);
    } catch {
      setItems([]);
    }
    setLoading(false);
  };

  useEffect(() => { loadItems(); }, [projectId]);

  const handleAction = async (action: "approve" | "reject", quality?: number) => {
    const item = items[index];
    if (!item) return;
    try {
      await api.post(`/api/projects/${projectId}/reviews/domain_rules`, {
        action,
        rule_uri: item.rule_uri,
        quality: quality || 2,
        category: action === "reject" ? "incorrect_logic" : undefined,
      });
      setStats((prev) => ({
        ...prev,
        [action === "approve" ? "approved" : "rejected"]: prev[action === "approve" ? "approved" : "rejected"] + 1,
      }));
    } catch { /* ignore */ }

    if (index + 1 < items.length) {
      setIndex(index + 1);
    } else {
      onReviewComplete();
      loadItems();
    }
  };

  if (loading) return <div style={{ color: "#94a3b8", fontSize: 13 }}>Loading domain rules...</div>;
  if (items.length === 0) return <div style={{ color: "#64748b", fontSize: 13 }}>No domain rules pending review.</div>;

  const item = items[index];

  return (
    <div style={{ backgroundColor: "#fff", borderRadius: 10, border: "1px solid #e2e8f0", padding: 20 }}>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 16 }}>
        <div style={{ fontWeight: 700, fontSize: 16, color: "#1e293b" }}>Domain Rule Review</div>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <span style={{ fontSize: 12, color: "#94a3b8" }}>
            {index + 1} of {items.length} &middot; {stats.approved} approved, {stats.rejected} rejected
          </span>
          <span
            onClick={async () => {
              const remaining = items.length - index;
              if (!(await confirm({
                title: "Approve all remaining",
                message: `Approve all ${remaining} remaining domain rules as "Good"?`,
                confirmLabel: "Approve all",
              }))) return;
              try {
                await api.post(`/api/projects/${projectId}/reviews/domain_rules/approve-all`, { quality: 2 });
                onReviewComplete();
                loadItems();
              } catch { /* ignore */ }
            }}
            style={{
              fontSize: 11, color: "#94a3b8", cursor: "pointer",
              textDecoration: "underline", fontWeight: 500,
            }}
            title="Approve all remaining domain rules with 'Good' quality rating"
          >
            Approve all remaining
          </span>
        </div>
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 10, marginBottom: 20 }}>
        <div style={{ display: "flex", gap: 16, fontSize: 13 }}>
          <span style={{ color: "#64748b" }}>Table:</span>
          <span style={{ fontWeight: 600 }}>{item.schema}.{item.table_name}</span>
        </div>
        <div style={{ display: "flex", gap: 16, fontSize: 13 }}>
          <span style={{ color: "#64748b" }}>Column:</span>
          <span style={{ fontWeight: 600 }}>{item.col_name}</span>
          <span style={{ color: "#94a3b8" }}>({item.data_type})</span>
        </div>
        <div style={{ display: "flex", gap: 16, fontSize: 13 }}>
          <span style={{ color: "#64748b" }}>Rule Type:</span>
          <span style={{
            padding: "2px 8px", borderRadius: 4, fontSize: 12, fontWeight: 600,
            backgroundColor: "#e0e7ff", color: "#4338ca",
          }}>
            {item.rule_type}
          </span>
          <span style={{
            padding: "2px 8px", borderRadius: 4, fontSize: 12, fontWeight: 600,
            backgroundColor: item.severity === "sh:Violation" ? "#fee2e2" : "#fef3c7",
            color: item.severity === "sh:Violation" ? "#dc2626" : "#92400e",
          }}>
            {item.severity}
          </span>
        </div>
        <div style={{
          padding: "12px 16px",
          backgroundColor: "#f8fafc",
          borderRadius: 8,
          border: "1px solid #e2e8f0",
          fontSize: 14,
          color: "#334155",
          lineHeight: 1.5,
        }}>
          {item.description}
        </div>
        {item.confidence != null && (
          <div style={{ fontSize: 12, color: "#94a3b8" }}>
            Confidence: {Math.round(item.confidence * 100)}%
          </div>
        )}
      </div>

      <div style={{ display: "flex", gap: 10 }}>
        <button
          onClick={() => handleAction("approve", 3)}
          style={{
            padding: "8px 20px", borderRadius: 6, border: "none",
            backgroundColor: "#22c55e", color: "#fff", fontSize: 13,
            fontWeight: 600, cursor: "pointer",
          }}
        >
          Approve
        </button>
        <button
          onClick={() => handleAction("reject")}
          style={{
            padding: "8px 20px", borderRadius: 6, border: "1px solid #fecaca",
            backgroundColor: "#fff", color: "#dc2626", fontSize: 13,
            fontWeight: 600, cursor: "pointer",
          }}
        >
          Reject
        </button>
        <button
          onClick={() => {
            if (index + 1 < items.length) setIndex(index + 1);
          }}
          disabled={index + 1 >= items.length}
          style={{
            padding: "8px 20px", borderRadius: 6, border: "1px solid #cbd5e1",
            backgroundColor: "#fff", color: "#64748b", fontSize: 13,
            fontWeight: 600, cursor: index + 1 < items.length ? "pointer" : "not-allowed",
            opacity: index + 1 < items.length ? 1 : 0.5,
          }}
        >
          Skip
        </button>
      </div>
    </div>
  );
}
