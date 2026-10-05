// Marketplace free-form chat drawer — Phase 3b.
//
// Right-side drawer that opens from the marketplace header. Required
// domain selector + optional product selector scope the chat. Stateless
// backend: each turn POSTs the full conversation history; backend gathers
// deployed views + concepts + sample rows for the scope, calls the
// marketplace-product-chat-assistant skill, validates the SQL through
// the allow-list + sql_executor, returns the result.
//
// Conversation is persisted in localStorage keyed by domain+product so
// switching scopes preserves separate threads. No server-side session
// storage in v1.

import { useEffect, useMemo, useState, type CSSProperties } from "react";
import api from "../api/client";
import MarkdownMessage from "./chat/MarkdownMessage";
import VegaChart from "./VegaChart";
import ProductKindChip from "./ProductKindChip";
import { useConfirm } from "./dialogContext";

interface Props {
  open: boolean;
  onClose: () => void;
  // Optional prefill: when opened from a specific product detail page,
  // the parent can pass these so the user doesn't have to re-pick.
  initialDomain?: string;
  initialContractId?: string;
}

interface ResponseColumn { name: string; dataType: string; }

interface ColumnBinding {
  ref: string;
  product_kind: "consumer" | "source" | "unknown" | string;
}

interface ConceptUsed {
  concept_uri: string;
  concept_name: string;
  values_used: string[];
  columns_bound: ColumnBinding[];
  role: string;
}

interface DisambiguationCandidate {
  label: string; value: string;
  column_name: string; view_name: string; view_schema: string;
}
interface DisambiguationOption {
  label: string; value?: string;
  column_name: string; view_name: string; view_schema: string;
}
interface Disambiguation {
  kind: "record" | "attribute" | "not_found";
  mention_text: string;
  prompt: string;
  candidates?: DisambiguationCandidate[];
  attribute_options?: DisambiguationOption[];
}
interface ResolvedValue {
  mention_text: string; value: string;
  column_name: string; view_name: string; view_schema: string;
}

interface AssistantPayload {
  status: "ok" | "failed" | "refused" | "needs_disambiguation";
  message?: string;
  // Record-level value resolution: present when a named value ("John Doe")
  // couldn't be uniquely matched against live data — the UI renders candidate /
  // attribute chips and re-submits the pick via resolved_values.
  disambiguation?: Disambiguation;
  sql?: string;
  columns?: ResponseColumn[];
  rows?: unknown[][];
  truncated?: boolean;
  row_count?: number;
  duration_ms?: number;
  explanation?: string;
  aggregation_kind?: string;
  confidence?: string;
  refused_reason?: string;
  error_class?: string;
  error_message?: string;
  used_views?: string[];
  concepts_used?: ConceptUsed[];
  fallback_reason?: string | null;
  suggested_questions?: string[];
  // Two-phase synthesis (Phase 3) + Concept-Guided retrieval (Phase 2).
  synthesized_answer?: string;
  chart_spec?: Record<string, unknown> | null;
  retrieval_mode?: "full" | "concept_guided";
  matched_concepts?: { uri: string; name: string | null; score: number | null; via: string }[];
  concept_fallback_reason?: string | null;
  // How many concepts were sent to the model: full = all domain entities;
  // concept_guided = the retrieved subset (or full set on fallback).
  concepts_in_context?: number;
  // Working tokens (uncached input + output) summed across this question's LLM
  // passes. cache_* are the cached prompt overhead, tracked for cost context.
  token_usage?: {
    input_tokens?: number;
    output_tokens?: number;
    total_tokens?: number;
    cache_read_tokens?: number;
    cache_creation_tokens?: number;
    cost_usd?: number | null;
  };
  // Ordered, adaptive resolution trace driving the "Explain" panel. Stateless —
  // assembled per-response, not persisted. Concept-guided adds decompose / match
  // / resolve steps; both modes add generate_sql / execute.
  trace?: Trace;
}

interface TraceMatchedConcept { uri: string; name: string | null; score?: number | null; via: string; }
interface TraceResolvedView {
  physical_name?: string; view_name?: string; product_name?: string;
  product_kind?: string; from_concepts?: string[];
}
interface TraceSelectedProduct {
  view_name?: string; product_name?: string; product_kind?: string; from_concepts?: string[];
}
interface TraceStepDetail {
  phrases?: string[];
  concepts?: TraceMatchedConcept[];
  views?: TraceResolvedView[];
  fallback_reason?: string | null;
  selected?: TraceSelectedProduct[];
  candidate_count?: number;
  sql?: string | null;
  explanation?: string;
  aggregation_kind?: string;
  confidence?: string;
  concepts_used?: ConceptUsed[];
  status?: string | null;
  row_count?: number | null;
  duration_ms?: number | null;
  truncated?: boolean | null;
  used_views?: string[];
  // Conversational routing step (the orchestrator "above" the retrieval modes).
  action?: ConvAction;
  matched_concepts?: string[];
  notes?: string;
  resolved_question?: string;
  ontology_concepts?: number;
  // Record-level value resolution step (ground_values).
  mentions?: {
    mention_text?: string; type_hint?: string; status?: string;
    column?: string; view?: string; candidate_count?: number;
    fuzzy_top?: number | null; resolved_value?: string | null;
  }[];
}
interface TraceStep { key: string; label: string; detail: TraceStepDetail; }
interface Trace { retrieval_mode?: "full" | "concept_guided" | "conversational"; steps: TraceStep[]; }

type ConvAction = "query" | "clarify" | "reject" | "chat";

interface TokenUsage {
  input_tokens?: number;
  output_tokens?: number;
  total_tokens?: number;
  cache_read_tokens?: number;
  cache_creation_tokens?: number;
  cost_usd?: number | null;
}

interface Message {
  role: "user" | "assistant";
  content: string;
  payload?: AssistantPayload;  // query turns (single-shot + conversational query)
  // Conversational-mode fields (clarify / reject / chat turns have no payload).
  action?: ConvAction;
  clarificationOptions?: string[];
  convTrace?: Trace;           // routing-only trace for non-query turns
  tokenUsage?: TokenUsage;     // per-turn total (router + optional query)
  // The originating question for a stateless needs_disambiguation turn, so a
  // candidate-chip click can re-submit it with the resolved pick.
  originQuestion?: string;
}

interface ProductSummary {
  uri: string;
  contract_id: string;
  name: string;
  domain: string;
  product_kind: string;
}

const STORAGE_KEY = (domain: string, contractId: string) =>
  `marketplace-chat:${domain}:${contractId || "__all__"}`;

// ── Explain panel (replaces the old "Show SQL"): renders the resolution trace ──

const explainStyles: Record<string, CSSProperties> = {
  summary: { cursor: "pointer", fontSize: 11, color: "#64748b", fontWeight: 600 },
  body: { marginTop: 6, display: "flex", flexDirection: "column", gap: 8,
    paddingLeft: 8, borderLeft: "2px solid #e2e8f0" },
  step: { display: "flex", flexDirection: "column", gap: 3 },
  label: { fontSize: 10, fontWeight: 700, textTransform: "uppercase", color: "#475569",
    letterSpacing: 0.3 },
  empty: { fontSize: 11, color: "#94a3b8" },
  meta: { fontSize: 11, color: "#64748b" },
  fallback: { fontSize: 11, color: "#9a3412", marginTop: 2 },
};

function ConceptUsedList({ concepts }: { concepts: ConceptUsed[] }) {
  const kindStyles: Record<string, { bg: string; fg: string }> = {
    consumer: { bg: "#dcfce7", fg: "#166534" },
    source: { bg: "#fed7aa", fg: "#9a3412" },
    unknown: { bg: "#e5e7eb", fg: "#374151" },
  };
  return (
    <>
      {concepts.map((c, ci) => (
        <div key={ci} style={ci === 0 ? { display: "flex", flexDirection: "column", gap: 2 } : styles.conceptRow}>
          <div>
            {c.role && <span style={styles.roleBadge}>{c.role}</span>}
            <span style={styles.conceptName}>{c.concept_name || "(unnamed concept)"}</span>
          </div>
          {c.values_used.length > 0 && (
            <div style={styles.conceptDetail}>
              values:&nbsp;
              {c.values_used.map((v, vi) => (<span key={vi} style={styles.valuePill}>{v}</span>))}
            </div>
          )}
          {c.columns_bound.length > 0 && (
            <div style={styles.conceptDetail}>
              bound to:&nbsp;
              {c.columns_bound.map((cb, cbi) => {
                const ks = kindStyles[cb.product_kind] || kindStyles.unknown;
                return (
                  <span key={cbi} style={{ marginRight: 4 }}>
                    <span style={styles.valuePill}>{cb.ref}</span>
                    <span style={{ ...styles.kindChip, backgroundColor: ks.bg, color: ks.fg }}>{cb.product_kind}</span>
                  </span>
                );
              })}
            </div>
          )}
        </div>
      ))}
    </>
  );
}

function ExplainStep({ step }: { step: TraceStep }) {
  const d = step.detail || {};
  return (
    <div style={explainStyles.step}>
      <div style={explainStyles.label}>{step.label}</div>
      {step.key === "routing" && (
        <div>
          <div style={styles.conceptDetail}>
            <span style={{ ...styles.kindChip, backgroundColor: "#e0e7ff", color: "#3730a3" }}>
              {d.action || "?"}
            </span>
            {typeof d.ontology_concepts === "number" && (
              <span style={explainStyles.meta}>&nbsp;grounded on {d.ontology_concepts} ontology concept{d.ontology_concepts === 1 ? "" : "s"}</span>
            )}
          </div>
          {(d.matched_concepts || []).length > 0 && (
            <div style={styles.conceptDetail}>
              concepts:&nbsp;
              {(d.matched_concepts || []).map((c, i) => (<span key={i} style={styles.valuePill}>{c}</span>))}
            </div>
          )}
          {d.notes && <div style={explainStyles.meta}>{d.notes}</div>}
          {d.action === "query" && d.resolved_question && (
            <div style={explainStyles.meta}>resolved: “{d.resolved_question}”</div>
          )}
        </div>
      )}
      {step.key === "decompose" && (
        (d.phrases || []).length > 0
          ? <div style={styles.conceptDetail}>{(d.phrases || []).map((p, i) => (<span key={i} style={styles.valuePill}>{p}</span>))}</div>
          : <div style={explainStyles.empty}>—</div>
      )}
      {step.key === "match_concepts" && (
        (d.concepts || []).length > 0
          ? (
            <div style={styles.conceptDetail}>
              {(d.concepts || []).map((c, i) => {
                // Direct vector match → show the similarity score (the "match"
                // tag was redundant). Graph-expansion entries → "related".
                const isNeighbor = c.via === "neighbor";
                const hasScore = typeof c.score === "number";
                const label = hasScore ? (c.score as number).toFixed(2) : isNeighbor ? "related" : "match";
                return (
                  <span key={i} style={{ marginRight: 4 }} title={
                    hasScore ? "Cosine similarity to the question"
                      : isNeighbor ? "Pulled in as a related concept (1-hop in the join graph)" : ""
                  }>
                    <span style={styles.valuePill}>{c.name || c.uri}</span>
                    <span style={{ ...styles.kindChip,
                      backgroundColor: isNeighbor ? "#ede9fe" : "#dbeafe",
                      color: isNeighbor ? "#6d28d9" : "#1e40af" }}>
                      {label}
                    </span>
                  </span>
                );
              })}
            </div>
          )
          : <div style={explainStyles.empty}>no concepts matched</div>
      )}
      {step.key === "resolve_views" && (
        <div>
          {(d.views || []).length > 0
            ? (d.views || []).map((v, i) => (
              <div key={i} style={styles.conceptDetail}>
                <span style={styles.conceptName}>{v.view_name || v.physical_name}</span>
                {v.product_name && <span style={{ color: "#64748b" }}> · {v.product_name}</span>}
                {v.product_kind && <>&nbsp;<ProductKindChip kind={v.product_kind} compact /></>}
                {(v.from_concepts || []).length > 0 && (
                  <span>&nbsp;{(v.from_concepts || []).map((fc, fi) => (<span key={fi} style={styles.valuePill}>{fc}</span>))}</span>
                )}
              </div>
            ))
            : <div style={explainStyles.empty}>—</div>}
          {d.fallback_reason && <div style={explainStyles.fallback}>Fell back: {d.fallback_reason}</div>}
        </div>
      )}
      {step.key === "select_product" && (
        <div>
          {(d.selected || []).length > 0
            ? (d.selected || []).map((s, i) => (
              <div key={i} style={styles.conceptDetail}>
                <span style={styles.conceptName}>{s.product_name || s.view_name}</span>
                {s.product_kind && <>&nbsp;<ProductKindChip kind={s.product_kind} compact /></>}
                {s.view_name && <span style={{ color: "#64748b" }}> · {s.view_name}</span>}
                {(s.from_concepts || []).length > 0 && (
                  <div style={explainStyles.meta}>
                    provides:&nbsp;
                    {(s.from_concepts || []).map((fc, fi) => (<span key={fi} style={styles.valuePill}>{fc}</span>))}
                  </div>
                )}
              </div>
            ))
            : <div style={explainStyles.empty}>—</div>}
          {typeof d.candidate_count === "number" && (
            <div style={explainStyles.meta}>
              selected from {d.candidate_count} candidate product{d.candidate_count === 1 ? "" : "s"}
            </div>
          )}
        </div>
      )}
      {step.key === "generate_sql" && (
        <div>
          {d.explanation && <div style={styles.conceptDetail}>{d.explanation}</div>}
          {(d.aggregation_kind || d.confidence) && (
            <div style={explainStyles.meta}>
              {d.aggregation_kind}{d.aggregation_kind && d.confidence ? " · " : ""}
              {d.confidence ? `confidence: ${d.confidence}` : ""}
            </div>
          )}
          {d.sql && <pre style={{ ...styles.sqlPre, marginTop: 6 }}>{d.sql}</pre>}
          {(d.concepts_used || []).length > 0 && (
            <div style={styles.conceptsCard}>
              <div style={styles.conceptsHeader}>Concepts referenced ({(d.concepts_used || []).length})</div>
              <ConceptUsedList concepts={d.concepts_used || []} />
            </div>
          )}
        </div>
      )}
      {step.key === "ground_values" && (
        <div>
          {(d.mentions || []).length > 0
            ? (d.mentions || []).map((mn, i) => (
              <div key={i} style={styles.conceptDetail}>
                <span style={styles.conceptName}>{mn.mention_text}</span>
                {mn.status === "resolved" && mn.view && mn.column ? (
                  <span style={{ color: "#64748b" }}>
                    {" → "}{mn.view}.{mn.column} = <span style={styles.valuePill}>{mn.resolved_value}</span>
                  </span>
                ) : (
                  <span style={{ ...styles.kindChip, backgroundColor: "#fef3c7", color: "#92400e" }}>
                    &nbsp;{(mn.status || "").replace(/_/g, " ")}
                    {typeof mn.candidate_count === "number" && mn.candidate_count > 0
                      ? ` · ${mn.candidate_count}` : ""}
                  </span>
                )}
              </div>
            ))
            : <div style={explainStyles.empty}>—</div>}
        </div>
      )}
      {step.key === "execute" && (
        <div style={explainStyles.meta}>
          {d.status}
          {typeof d.row_count === "number" ? ` · ${d.row_count} row${d.row_count === 1 ? "" : "s"}` : ""}
          {typeof d.duration_ms === "number" ? ` · ${d.duration_ms}ms` : ""}
          {(d.used_views || []).length > 0 ? ` · ${(d.used_views || []).join(", ")}` : ""}
          {d.truncated ? " · truncated" : ""}
        </div>
      )}
    </div>
  );
}

function ExplainPanel({ pl }: { pl: AssistantPayload }) {
  const steps = pl.trace?.steps || [];
  // Back-compat: responses predating the trace still surface bare SQL.
  if (steps.length === 0) {
    return pl.sql ? (
      <details style={{ marginTop: 8 }}>
        <summary style={explainStyles.summary}>Show SQL</summary>
        <pre style={{ ...styles.sqlPre, marginTop: 6 }}>{pl.sql}</pre>
      </details>
    ) : null;
  }
  return (
    <details style={{ marginTop: 8 }}>
      <summary style={explainStyles.summary}>Explain — how this answer was resolved</summary>
      <div style={explainStyles.body}>
        {steps.map((s, i) => (<ExplainStep key={i} step={s} />))}
      </div>
    </details>
  );
}

const styles: Record<string, CSSProperties> = {
  overlay: {
    position: "fixed", inset: 0, backgroundColor: "rgba(15,23,42,0.3)",
    zIndex: 100, display: "flex", justifyContent: "flex-end",
  },
  drawer: {
    width: 560, maxWidth: "92vw", height: "100vh",
    backgroundColor: "#fff", boxShadow: "-4px 0 20px rgba(0,0,0,0.15)",
    display: "flex", flexDirection: "column",
  },
  header: {
    padding: "16px 20px", borderBottom: "1px solid #e2e8f0",
    display: "flex", alignItems: "center", justifyContent: "space-between",
  },
  title: { fontSize: 16, fontWeight: 700, color: "#0f172a" },
  closeBtn: {
    fontSize: 13, padding: "4px 10px", border: "1px solid #cbd5e1",
    borderRadius: 5, backgroundColor: "#fff", color: "#334155", cursor: "pointer",
  },
  scopeBar: {
    padding: "10px 20px", borderBottom: "1px solid #e2e8f0",
    display: "grid", gridTemplateColumns: "1fr 1fr auto", gap: 8, alignItems: "center",
    backgroundColor: "#f8fafc",
  },
  select: {
    fontSize: 13, padding: "5px 8px", border: "1px solid #cbd5e1",
    borderRadius: 6, backgroundColor: "#fff", color: "#0f172a",
  },
  newBtn: {
    fontSize: 11, padding: "4px 8px", border: "1px solid #cbd5e1", borderRadius: 5,
    backgroundColor: "#fff", color: "#334155", cursor: "pointer", fontWeight: 600,
  },
  retrievalBar: {
    padding: "6px 20px", borderBottom: "1px solid #e2e8f0",
    display: "flex", alignItems: "center", gap: 8, backgroundColor: "#f8fafc",
  },
  retrievalLabel: {
    fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: 0.4,
    color: "#64748b",
  },
  segmented: {
    display: "inline-flex", border: "1px solid #cbd5e1", borderRadius: 6, overflow: "hidden",
  },
  segActive: {
    fontSize: 11, padding: "4px 10px", border: "none", cursor: "pointer",
    backgroundColor: "#3b82f6", color: "#fff", fontWeight: 700,
  },
  segIdle: {
    fontSize: 11, padding: "4px 10px", border: "none", cursor: "pointer",
    backgroundColor: "#fff", color: "#334155", fontWeight: 600,
  },
  guidedChip: {
    display: "inline-block", fontSize: 10, fontWeight: 700, padding: "1px 6px",
    borderRadius: 4, backgroundColor: "#ede9fe", color: "#6d28d9", marginTop: 6,
    cursor: "help",
  },
  // Full Context variant — neutral slate so the two modes are visually distinct
  // but read as the same kind of badge.
  fullChip: {
    display: "inline-block", fontSize: 10, fontWeight: 700, padding: "1px 6px",
    borderRadius: 4, backgroundColor: "#e2e8f0", color: "#334155", marginTop: 6,
    cursor: "help",
  },
  messages: { flex: 1, overflowY: "auto", padding: "16px 20px", display: "flex", flexDirection: "column", gap: 12 },
  userBubble: {
    alignSelf: "flex-end", maxWidth: "85%",
    padding: "10px 14px", borderRadius: 14,
    backgroundColor: "#3b82f6", color: "#fff", fontSize: 13, lineHeight: 1.5,
  },
  assistantBubble: {
    alignSelf: "flex-start", maxWidth: "92%",
    padding: "10px 14px", borderRadius: 14,
    backgroundColor: "#f1f5f9", color: "#0f172a", fontSize: 13, lineHeight: 1.5,
    whiteSpace: "pre-wrap",
  },
  refusalCard: {
    alignSelf: "flex-start", maxWidth: "92%",
    padding: 10, borderRadius: 8,
    backgroundColor: "#fffbeb", color: "#92400e",
    border: "1px solid #fed7aa", fontSize: 12,
  },
  failedCard: {
    alignSelf: "flex-start", maxWidth: "92%",
    padding: 10, borderRadius: 8,
    backgroundColor: "#fef2f2", color: "#991b1b",
    border: "1px solid #fecaca", fontSize: 12,
  },
  disambiguationCard: {
    alignSelf: "flex-start", maxWidth: "92%",
    padding: 10, borderRadius: 8,
    backgroundColor: "#eff6ff", color: "#1e3a8a",
    border: "1px solid #bfdbfe", fontSize: 12,
  },
  sqlPre: {
    margin: 0, padding: 10,
    backgroundColor: "#0f172a", color: "#a5f3fc",
    fontSize: 11, lineHeight: 1.5, overflow: "auto",
    fontFamily: "'Fira Code', monospace",
    borderRadius: 6, whiteSpace: "pre-wrap",
  },
  resultTable: {
    width: "100%", borderCollapse: "collapse", fontSize: 11,
    fontFamily: "'Fira Code', monospace",
  },
  resultMeta: { fontSize: 11, color: "#64748b", marginTop: 6 },
  suggestionsRow: { display: "flex", flexWrap: "wrap", gap: 6, marginTop: 8 },
  suggestionChip: {
    fontSize: 11, padding: "3px 8px", borderRadius: 12,
    backgroundColor: "#dbeafe", color: "#1e40af", border: "none",
    cursor: "pointer", fontWeight: 600,
  },
  conceptsCard: {
    marginTop: 8, padding: "8px 10px",
    borderRadius: 6,
    backgroundColor: "#f5f3ff", border: "1px solid #ddd6fe",
    fontSize: 11, lineHeight: 1.5, color: "#312e81",
  },
  conceptsHeader: {
    fontWeight: 700, fontSize: 10, textTransform: "uppercase",
    letterSpacing: 0.4, color: "#6d28d9", marginBottom: 4,
  },
  conceptRow: {
    display: "flex", flexDirection: "column", gap: 2,
    paddingTop: 4, marginTop: 4,
    borderTop: "1px dashed #ddd6fe",
  },
  conceptName: { fontWeight: 700, color: "#0f172a" },
  conceptDetail: { color: "#475569" },
  valuePill: {
    display: "inline-block", padding: "0 6px", marginRight: 4,
    backgroundColor: "#ede9fe", borderRadius: 4,
    fontFamily: "'Fira Code', monospace", fontSize: 10, color: "#5b21b6",
  },
  roleBadge: {
    display: "inline-block", padding: "0 5px", marginRight: 6,
    fontSize: 9, fontWeight: 700, textTransform: "uppercase",
    backgroundColor: "#c4b5fd", color: "#312e81", borderRadius: 3,
  },
  kindChip: {
    display: "inline-block", padding: "0 4px", marginLeft: 4,
    fontSize: 9, fontWeight: 700, textTransform: "uppercase",
    borderRadius: 3, verticalAlign: "middle",
  },
  fallbackCard: {
    marginTop: 8, padding: "8px 10px",
    borderRadius: 6,
    backgroundColor: "#fef3c7", border: "1px solid #fcd34d",
    fontSize: 11, lineHeight: 1.5, color: "#78350f",
  },
  fallbackHeader: {
    fontWeight: 700, fontSize: 10, textTransform: "uppercase",
    letterSpacing: 0.4, color: "#a16207", marginBottom: 4,
  },
  inputBar: {
    borderTop: "1px solid #e2e8f0",
    padding: "12px 20px", display: "flex", gap: 8,
  },
  textarea: {
    flex: 1, padding: "8px 10px", border: "1px solid #cbd5e1", borderRadius: 6,
    fontSize: 13, minHeight: 56, resize: "vertical", fontFamily: "inherit",
  },
  sendBtn: {
    padding: "8px 16px", border: "none", borderRadius: 6,
    backgroundColor: "#3b82f6", color: "#fff", cursor: "pointer", fontWeight: 600, fontSize: 13,
  },
};

// Tasteful "agent is working" indicator — three softly-bouncing dots next to a
// label, matching the ProductChatPanel `pa-dot` style. Injected once via the
// <style> tag in the drawer so the inline styles can reference the keyframe.
const SEMANTIC_CHAT_KEYFRAMES = `
@keyframes mc-dot {
  0%, 80%, 100% { transform: scale(0.5); opacity: 0.35; }
  40% { transform: scale(1); opacity: 1; }
}
@keyframes mc-fade {
  0%, 100% { opacity: 0.55; }
  50% { opacity: 1; }
}`;

function ThinkingIndicator({ label }: { label: string }) {
  const dot: CSSProperties = {
    display: "inline-block", width: 6, height: 6, borderRadius: "50%",
    background: "#6366f1", animation: "mc-dot 1.2s ease-in-out infinite",
  };
  return (
    <div style={{ ...styles.assistantBubble, display: "flex", alignItems: "center", gap: 8 }}>
      <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
        <span style={{ ...dot, animationDelay: "0s" }} />
        <span style={{ ...dot, animationDelay: "0.15s" }} />
        <span style={{ ...dot, animationDelay: "0.3s" }} />
      </span>
      <span style={{ fontStyle: "italic", color: "#64748b", animation: "mc-fade 1.6s ease-in-out infinite" }}>
        {label}
      </span>
    </div>
  );
}

export default function MarketplaceChatPanel({ open, onClose, initialDomain, initialContractId }: Props) {
  const confirm = useConfirm();
  const [domains, setDomains] = useState<string[]>([]);
  const [products, setProducts] = useState<ProductSummary[]>([]);
  const [domain, setDomain] = useState(initialDomain || "");
  const [contractId, setContractId] = useState(initialContractId || "");
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The mode toggle: "full" / "concept_guided" are the single-shot retrieval
  // strategies (existing behaviour); "conversational" is the orchestrator
  // ABOVE them — a server-side stateful agent that decides per turn whether to
  // query / clarify / reject / chat, using `convoSubmode` as its retrieval
  // strategy when it DOES query.
  const [uiMode, setUiMode] = useState<"full" | "concept_guided" | "conversational">("full");
  const [convoSubmode, setConvoSubmode] = useState<"full" | "concept_guided">("concept_guided");
  const [sessionId, setSessionId] = useState<number | null>(null);
  const [reportedGaps, setReportedGaps] = useState<Record<number, "done" | "failed">>({});
  const conversational = uiMode === "conversational";

  const reportGap = async (i: number, pl: AssistantPayload) => {
    const question = messages[i - 1]?.role === "user" ? messages[i - 1].content : "";
    try {
      await api.post("/api/marketplace/gaps", {
        domain,
        contract_id: contractId || undefined,
        question,
        refused_reason: pl.refused_reason || pl.error_message,
        analysis: pl.message,
        concepts_used: pl.concepts_used,
        retrieval_meta: {
          retrieval_mode: pl.retrieval_mode,
          matched_concepts: pl.matched_concepts,
          concept_fallback_reason: pl.concept_fallback_reason,
        },
      });
      setReportedGaps((p) => ({ ...p, [i]: "done" }));
    } catch {
      setReportedGaps((p) => ({ ...p, [i]: "failed" }));
    }
  };

  useEffect(() => {
    if (!open) return;
    // Domain list comes from the actual products' :Project.domain values
    // (snake-case slugs like "products_sales"). /api/domains now sources
    // from the same place, but reading the marketplace gives us the full
    // product list at the same time for the second dropdown.
    api.get("/api/marketplace")
      .then((r) => {
        const list = (r.data?.products as ProductSummary[]) || [];
        setProducts(list);
        const uniq = Array.from(new Set(list.map((p) => p.domain).filter(Boolean))).sort();
        setDomains(uniq);
      })
      .catch(() => { setProducts([]); setDomains([]); });
  }, [open]);

  // Reset/load conversation when scope or mode changes. Conversational mode is
  // server-session backed (no localStorage); switching scope/mode starts a fresh
  // session lazily on the next send.
  useEffect(() => {
    if (conversational) { setMessages([]); setSessionId(null); return; }
    if (!domain) { setMessages([]); return; }
    try {
      const raw = window.localStorage.getItem(STORAGE_KEY(domain, contractId));
      if (raw) {
        setMessages(JSON.parse(raw));
        return;
      }
    } catch {
      // localStorage may throw in private-mode browsers
    }
    setMessages([]);
  }, [domain, contractId, conversational]);

  // Persist conversation on change (single-shot mode only — conversational lives
  // server-side and its payloads are too bulky for localStorage).
  useEffect(() => {
    if (conversational || !domain) return;
    try {
      window.localStorage.setItem(STORAGE_KEY(domain, contractId), JSON.stringify(messages));
    } catch {
      /* localStorage write may throw */
    }
  }, [messages, domain, contractId, conversational]);

  const filteredProducts = useMemo(
    () => products.filter((p) => !domain || p.domain === domain),
    [products, domain],
  );

  const sendMessage = async (overrideMessage?: string) => {
    const userText = (overrideMessage ?? input).trim();
    if (!userText || sending || !domain) return;

    setMessages((prev) => [...prev, { role: "user", content: userText }]);
    setInput("");
    setSending(true);
    setError(null);

    if (conversational) {
      // Server-session backed orchestrator: the backend owns the transcript +
      // rolling memory, so we send only the new turn + the session id.
      try {
        const res = await api.post("/api/marketplace/conversation", {
          session_id: sessionId,
          domain,
          contract_id: contractId || undefined,
          user_message: userText,
          retrieval_submode: convoSubmode,
          limit: 100,
        });
        const data = res.data as {
          session_id: number; action: ConvAction; reply?: string;
          payload?: AssistantPayload; trace?: Trace;
          clarification_options?: string[]; token_usage?: TokenUsage;
        };
        if (data.session_id) setSessionId(data.session_id);
        const assistantMessage: Message =
          data.action === "query"
            ? {
                role: "assistant",
                content: data.payload?.synthesized_answer || data.payload?.message || data.reply || "(no message)",
                payload: data.payload,
                action: "query",
                tokenUsage: data.token_usage,
              }
            : {
                role: "assistant",
                content: data.reply || "(no message)",
                action: data.action,
                clarificationOptions: data.clarification_options || [],
                convTrace: data.trace,
                tokenUsage: data.token_usage,
              };
        setMessages((prev) => [...prev, assistantMessage]);
      } catch (e: unknown) {
        const err = e as { response?: { data?: { detail?: string } }; message?: string };
        const detail = err?.response?.data?.detail || err?.message || "Chat failed";
        setError(String(detail));
        setMessages((prev) => [
          ...prev,
          { role: "assistant", content: `(error: ${detail})`, payload: { status: "failed", error_message: detail } },
        ]);
      } finally {
        setSending(false);
      }
      return;
    }

    // Single-shot path (Full / Concept-Guided): stateless, client sends history.
    const conversationToSend = messages.map((m) => ({
      role: m.role,
      content: m.role === "assistant" && m.payload?.sql
        ? `${m.content}\n[SQL: ${m.payload.sql.substring(0, 200)}…]`
        : m.content,
    }));

    try {
      const res = await api.post("/api/marketplace/chat", {
        domain,
        contract_id: contractId || undefined,
        conversation: conversationToSend,
        user_message: userText,
        limit: 100,
        retrieval_mode: uiMode === "concept_guided" ? "concept_guided" : "full",
      });
      const payload = res.data as AssistantPayload;
      const assistantMessage: Message = {
        role: "assistant",
        content: payload.message || "(no message)",
        payload,
        originQuestion: userText,
      };
      setMessages((prev) => [...prev, assistantMessage]);
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string };
      const detail = err?.response?.data?.detail || err?.message || "Chat failed";
      setError(String(detail));
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: `(error: ${detail})`, payload: { status: "failed", error_message: detail } },
      ]);
    } finally {
      setSending(false);
    }
  };

  // Re-submit a stateless turn after the user picked a disambiguation candidate
  // (or attribute). The original question + the resolved pick ground the answer
  // deterministically (the backend skips extraction/probing). Conversational mode
  // doesn't use this — its disambiguation arrives as a `clarify` turn whose chips
  // route back through sendMessage(label) and reconcile server-side.
  const resubmitWithResolved = async (originQuestion: string, chosen: ResolvedValue, chosenLabel: string) => {
    if (sending || !domain || !originQuestion) return;
    setMessages((prev) => [...prev, { role: "user", content: chosenLabel }]);
    setSending(true);
    setError(null);
    const conversationToSend = messages.map((m) => ({
      role: m.role,
      content: m.role === "assistant" && m.payload?.sql
        ? `${m.content}\n[SQL: ${m.payload.sql.substring(0, 200)}…]`
        : m.content,
    }));
    try {
      const res = await api.post("/api/marketplace/chat", {
        domain,
        contract_id: contractId || undefined,
        conversation: conversationToSend,
        user_message: originQuestion,
        limit: 100,
        retrieval_mode: uiMode === "concept_guided" ? "concept_guided" : "full",
        resolved_values: [chosen],
      });
      const payload = res.data as AssistantPayload;
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: payload.message || "(no message)", payload, originQuestion },
      ]);
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string };
      const detail = err?.response?.data?.detail || err?.message || "Chat failed";
      setError(String(detail));
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: `(error: ${detail})`, payload: { status: "failed", error_message: detail } },
      ]);
    } finally {
      setSending(false);
    }
  };

  const newConversation = async () => {
    if (!(await confirm({
      title: "Start a new conversation",
      message: "Current history will be cleared from this scope.",
      confirmLabel: "Start new",
    }))) return;
    if (conversational) {
      const sid = sessionId;
      setMessages([]);
      setSessionId(null);
      if (sid) {
        try { await api.delete(`/api/marketplace/conversation/sessions/${sid}`); } catch { /* best-effort */ }
      }
      return;
    }
    setMessages([]);
    try {
      window.localStorage.removeItem(STORAGE_KEY(domain, contractId));
    } catch {
      /* localStorage delete may throw */
    }
  };

  if (!open) return null;

  return (
    <div style={styles.overlay} onClick={onClose}>
      <div style={styles.drawer} onClick={(e) => e.stopPropagation()}>
        <style>{SEMANTIC_CHAT_KEYFRAMES}</style>
        <div style={styles.header}>
          <div style={styles.title}>Semantic Q&amp;A</div>
          <button type="button" style={styles.closeBtn} onClick={onClose}>×</button>
        </div>

        <div style={styles.scopeBar}>
          <select style={styles.select} value={domain} onChange={(e) => setDomain(e.target.value)}>
            <option value="">— pick a domain —</option>
            {domains.map((d) => <option key={d} value={d}>{d}</option>)}
          </select>
          <select style={styles.select} value={contractId} onChange={(e) => setContractId(e.target.value)} disabled={!domain}>
            <option value="">All products in domain</option>
            {filteredProducts.map((p) => (
              <option key={p.contract_id} value={p.contract_id}>{p.name}</option>
            ))}
          </select>
          <button type="button" style={styles.newBtn} onClick={newConversation} disabled={messages.length === 0}>
            New
          </button>
        </div>

        <div style={styles.retrievalBar}>
          <span style={styles.retrievalLabel}>Mode</span>
          <div style={styles.segmented}>
            <button
              type="button"
              style={uiMode === "full" ? styles.segActive : styles.segIdle}
              onClick={() => setUiMode("full")}
              title="Single-shot: send every concept in the domain and let the model match"
            >
              Full Context
            </button>
            <button
              type="button"
              style={uiMode === "concept_guided" ? styles.segActive : styles.segIdle}
              onClick={() => setUiMode("concept_guided")}
              title="Single-shot: embedding search → only the matching concepts + their neighbours"
            >
              Concept-Guided
            </button>
            <button
              type="button"
              style={uiMode === "conversational" ? styles.segActive : styles.segIdle}
              onClick={() => setUiMode("conversational")}
              title="Conversational agent grounded on the domain ontology — decides when to query, asks clarifying questions, keeps session memory"
            >
              Conversational
            </button>
          </div>
        </div>
        {conversational && (
          <div style={styles.retrievalBar}>
            <span style={styles.retrievalLabel}>Queries with</span>
            <div style={styles.segmented}>
              <button
                type="button"
                style={convoSubmode === "concept_guided" ? styles.segActive : styles.segIdle}
                onClick={() => setConvoSubmode("concept_guided")}
                title="When the agent queries, retrieve only the matching concepts + neighbours"
              >
                Concept-Guided
              </button>
              <button
                type="button"
                style={convoSubmode === "full" ? styles.segActive : styles.segIdle}
                onClick={() => setConvoSubmode("full")}
                title="When the agent queries, send every concept in the domain"
              >
                Full Context
              </button>
            </div>
          </div>
        )}

        <div style={styles.messages}>
          {messages.length === 0 && (
            <div style={{ color: "#64748b", fontSize: 13, fontStyle: "italic", padding: 12 }}>
              {!domain
                ? "Pick a domain to start chatting."
                : conversational
                  ? "Ask a question and chat naturally. The agent is grounded on this domain's ontology — it decides when to query the data, asks for clarification when needed, and remembers the conversation."
                  : "Ask a question about the deployed views in this scope. The agent will author SQL and run it against the deployed views."}
            </div>
          )}
          {messages.map((m, i) => {
            if (m.role === "user") {
              return <div key={i} style={styles.userBubble}>{m.content}</div>;
            }
            // assistant
            const pl = m.payload;
            // Conversational non-query turns (clarify / reject / chat): no SQL —
            // a plain reply, optional clarification chips, + routing Explain.
            if (m.action && m.action !== "query" && !pl) {
              const isReject = m.action === "reject";
              return (
                <div key={i} style={isReject ? styles.refusalCard : styles.assistantBubble}>
                  {isReject && <div style={{ fontWeight: 600, marginBottom: 4 }}>Out of scope</div>}
                  <MarkdownMessage content={m.content} />
                  {(m.clarificationOptions || []).length > 0 && (
                    <div style={styles.suggestionsRow}>
                      {(m.clarificationOptions || []).map((q, qi) => (
                        <button key={qi} type="button" style={styles.suggestionChip} onClick={() => sendMessage(q)}>
                          {q}
                        </button>
                      ))}
                    </div>
                  )}
                  {m.convTrace && m.convTrace.steps.length > 0 && (
                    <ExplainPanel pl={{ status: "ok", trace: m.convTrace } as AssistantPayload} />
                  )}
                  {typeof m.tokenUsage?.total_tokens === "number" && m.tokenUsage.total_tokens > 0 && (
                    <div style={{ fontSize: 10, color: "#94a3b8", marginTop: 6 }}>
                      {m.tokenUsage.total_tokens} tok
                    </div>
                  )}
                </div>
              );
            }
            if (pl?.status === "needs_disambiguation" && pl.disambiguation) {
              const dis = pl.disambiguation;
              const origin = m.originQuestion || "";
              return (
                <div key={i} style={styles.disambiguationCard}>
                  <div style={{ fontWeight: 600, marginBottom: 6 }}>{dis.prompt}</div>
                  {dis.kind === "record" && (dis.candidates || []).length > 0 && (
                    <div style={styles.suggestionsRow}>
                      {(dis.candidates || []).map((c, ci) => (
                        <button key={ci} type="button" style={styles.suggestionChip}
                          onClick={() => resubmitWithResolved(origin, {
                            mention_text: dis.mention_text, value: c.value,
                            column_name: c.column_name, view_name: c.view_name, view_schema: c.view_schema,
                          }, c.label)}>
                          {c.label}
                        </button>
                      ))}
                    </div>
                  )}
                  {dis.kind === "attribute" && (dis.attribute_options || []).length > 0 && (
                    <div style={styles.suggestionsRow}>
                      {(dis.attribute_options || []).map((o, oi) => (
                        <button key={oi} type="button" style={styles.suggestionChip}
                          onClick={() => resubmitWithResolved(origin, {
                            mention_text: dis.mention_text, value: "",
                            column_name: o.column_name, view_name: o.view_name, view_schema: o.view_schema,
                          }, o.label)}>
                          {o.label}
                        </button>
                      ))}
                    </div>
                  )}
                  <ExplainPanel pl={pl} />
                </div>
              );
            }
            if (pl?.status === "refused") {
              return (
                <div key={i} style={styles.refusalCard}>
                  <div style={{ fontWeight: 600, marginBottom: 4 }}>I can't answer that</div>
                  <div>{pl.refused_reason}</div>
                  {pl.message && <div style={{ marginTop: 6 }}>{pl.message}</div>}
                  {(pl.suggested_questions || []).length > 0 && (
                    <div style={styles.suggestionsRow}>
                      {(pl.suggested_questions || []).map((q, qi) => (
                        <button key={qi} type="button" style={styles.suggestionChip} onClick={() => sendMessage(q)}>
                          {q}
                        </button>
                      ))}
                    </div>
                  )}
                  <ReportGap i={i} pl={pl} state={reportedGaps[i]} onReport={reportGap} />
                </div>
              );
            }
            if (pl?.status === "failed") {
              return (
                <div key={i} style={styles.failedCard}>
                  <div style={{ fontWeight: 600, marginBottom: 4 }}>
                    {pl.error_class || "Error"}
                  </div>
                  <div>{pl.error_message || m.content}</div>
                  {pl.sql && <pre style={{ ...styles.sqlPre, marginTop: 6 }}>{pl.sql}</pre>}
                  <ReportGap i={i} pl={pl} state={reportedGaps[i]} onReport={reportGap} />
                </div>
              );
            }
            // ok
            return (
              <div key={i} style={styles.assistantBubble}>
                {pl?.retrieval_mode && (() => {
                  // Symmetric, self-explanatory retrieval badge for BOTH modes:
                  //   "<mode> · <N> in scope/retrieved → <M> referenced"
                  // in scope/retrieved = concepts handed to the model;
                  // referenced = concepts the generated SQL actually used.
                  const guided = pl.retrieval_mode === "concept_guided";
                  const inCtx = pl.concepts_in_context ?? (pl.matched_concepts || []).length;
                  const referenced = (pl.concepts_used || []).length;
                  const fellBack = guided && !!pl.concept_fallback_reason;
                  const scopeText = fellBack
                    ? `no match, used all ${inCtx}`
                    : guided
                      ? `${inCtx} retrieved`
                      : `${inCtx} in scope`;
                  const tok = pl.token_usage?.total_tokens ?? 0;
                  return (
                    <div
                      style={guided ? styles.guidedChip : styles.fullChip}
                      title={
                        "In scope = concepts given to the model. " +
                        "Referenced = concepts the generated SQL actually used. " +
                        "Tokens = working (uncached) input + output across this question's passes — " +
                        "toggle the mode and re-ask to compare." +
                        (fellBack ? `\n\nFallback: ${pl.concept_fallback_reason}` : "")
                      }
                    >
                      {guided ? "Concept-Guided" : "Full Context"} · {scopeText} → {referenced} referenced
                      {tok > 0 ? ` · ${tok.toLocaleString()} tok` : ""} ⓘ
                    </div>
                  );
                })()}
                {pl?.synthesized_answer ? (
                  <div style={{ marginTop: 4 }}>
                    <MarkdownMessage content={pl.synthesized_answer} />
                  </div>
                ) : (
                  <div>{m.content}</div>
                )}
                {pl?.aggregation_kind && pl.aggregation_kind !== "raw" && (
                  <div style={{ marginTop: 6 }}>
                    <span style={{
                      fontSize: 10, padding: "1px 6px", borderRadius: 4,
                      backgroundColor: "#dbeafe", color: "#1e40af",
                      fontWeight: 700, textTransform: "uppercase",
                    }}>{pl.aggregation_kind}</span>
                    {pl.confidence && (
                      <span style={{ marginLeft: 6, fontSize: 10, color: "#64748b" }}>
                        confidence: {pl.confidence}
                      </span>
                    )}
                  </div>
                )}
                {pl?.fallback_reason && (
                  <div style={styles.fallbackCard}>
                    <div style={styles.fallbackHeader}>Fell back to source-aligned</div>
                    <div>{pl.fallback_reason}</div>
                  </div>
                )}
                <ExplainPanel pl={pl} />
                {pl?.columns && pl.rows && pl.rows.length > 0 && (
                  <div style={{ marginTop: 8, overflow: "auto", maxHeight: 280, border: "1px solid #e2e8f0", borderRadius: 6 }}>
                    <table style={styles.resultTable}>
                      <thead>
                        <tr>
                          {pl.columns.map((c, ci) => (
                            <th key={ci} style={{
                              position: "sticky", top: 0,
                              backgroundColor: "#f1f5f9", borderBottom: "1px solid #e2e8f0",
                              padding: "5px 8px", textAlign: "left",
                              fontWeight: 600, color: "#0f172a", whiteSpace: "nowrap",
                            }}>{c.name}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {pl.rows.map((r, ri) => (
                          <tr key={ri} style={{ backgroundColor: ri % 2 === 0 ? "#fff" : "#f8fafc" }}>
                            {(r as unknown[]).map((v, vi) => (
                              <td key={vi} style={{
                                padding: "4px 8px",
                                borderBottom: "1px solid #f1f5f9",
                                color: v === null ? "#94a3b8" : "#0f172a",
                                whiteSpace: "nowrap", maxWidth: 200,
                                overflow: "hidden", textOverflow: "ellipsis",
                              }} title={v === null ? "null" : String(v)}>
                                {v === null ? "null" : String(v)}
                              </td>
                            ))}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
                {pl?.chart_spec && (
                  <div style={{ marginTop: 8 }}>
                    <VegaChart spec={pl.chart_spec} />
                  </div>
                )}
                {pl?.row_count !== undefined && (
                  <div style={styles.resultMeta}>
                    {pl.row_count} row{pl.row_count === 1 ? "" : "s"}
                    {pl.duration_ms !== undefined && ` · ${pl.duration_ms}ms`}
                    {pl.used_views && pl.used_views.length > 0 && ` · ${pl.used_views.join(", ")}`}
                    {pl.truncated && ` · truncated`}
                    {pl.token_usage && (pl.token_usage.total_tokens ?? 0) > 0 && (
                      <span
                        title={
                          "Working tokens (uncached input + output) summed across this question's LLM passes." +
                          ((pl.token_usage.cache_read_tokens || pl.token_usage.cache_creation_tokens)
                            ? `\nCached prompt overhead: ${((pl.token_usage.cache_read_tokens || 0) + (pl.token_usage.cache_creation_tokens || 0)).toLocaleString()} tokens.`
                            : "")
                        }
                      >
                        {" · "}
                        {(pl.token_usage.total_tokens || 0).toLocaleString()} tokens
                        {" ("}
                        {(pl.token_usage.input_tokens || 0).toLocaleString()} in /{" "}
                        {(pl.token_usage.output_tokens || 0).toLocaleString()} out)
                      </span>
                    )}
                  </div>
                )}
                {(pl?.suggested_questions || []).length > 0 && (
                  <div style={styles.suggestionsRow}>
                    {(pl.suggested_questions || []).map((q, qi) => (
                      <button key={qi} type="button" style={styles.suggestionChip} onClick={() => sendMessage(q)}>
                        {q}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            );
          })}
          {sending && (
            <ThinkingIndicator label={conversational ? "Thinking…" : "Synthesising answer…"} />
          )}
          {error && (
            <div style={styles.failedCard}>{error}</div>
          )}
        </div>

        <div style={styles.inputBar}>
          <textarea
            style={styles.textarea}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder={domain ? "Ask anything about the deployed views in this scope…" : "Pick a domain first."}
            disabled={!domain || sending}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
                e.preventDefault();
                sendMessage();
              }
            }}
          />
          <button
            type="button"
            style={{ ...styles.sendBtn, opacity: (!domain || sending || !input.trim()) ? 0.5 : 1 }}
            onClick={() => sendMessage()}
            disabled={!domain || sending || !input.trim()}
          >
            {sending ? "…" : "Send"}
          </button>
        </div>
      </div>
    </div>
  );
}

function ReportGap({
  i, pl, state, onReport,
}: {
  i: number;
  pl: AssistantPayload;
  state?: "done" | "failed";
  onReport: (i: number, pl: AssistantPayload) => void;
}) {
  if (state === "done") {
    return <div style={{ marginTop: 8, fontSize: 11, color: "#166534", fontWeight: 600 }}>Reported ✓ — logged to Gaps</div>;
  }
  return (
    <div style={{ marginTop: 8 }}>
      <button
        type="button"
        onClick={() => onReport(i, pl)}
        style={{
          fontSize: 11, fontWeight: 600, padding: "3px 9px", borderRadius: 5,
          border: "1px solid #cbd5e1", backgroundColor: "#fff", color: "#475569", cursor: "pointer",
        }}
        title="Log this as a gap for the team to triage"
      >
        ⚑ Report this gap
      </button>
      {state === "failed" && (
        <span style={{ marginLeft: 8, fontSize: 11, color: "#b91c1c" }}>couldn't report — try again</span>
      )}
    </div>
  );
}
