import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import api from "../../api/client";
import { productTheme } from "../../theme";
import type { ChatWSMessage } from "../../types";
import { useSkillRegistry } from "../../lib/skillRegistry";
import { useProductChatSocket } from "../../hooks/useProductChatSocket";
import MarkdownMessage from "./MarkdownMessage";
import { formatToolUse } from "./toolUseLabel";
import AttachmentBar, { type AttachmentDraft } from "./AttachmentBar";
import { useConfirm } from "../dialogContext";

const DEFAULT_WIDTH = 440;
const MIN_WIDTH = 320;
const MAX_WIDTH = 900;
const WIDTH_STORAGE_KEY = "product_chat.drawerWidth";

export interface GuideMeRequest {
  prefill: string;
  autoSend?: boolean;
  nonce?: number;
  /** Wizard state passed to the agent out-of-band (kept out of the
   *  visible transcript). */
  context?: Record<string, unknown> | null;
}

/** Structured suggestion emitted by the skill — see
 *  product-authoring-assistant SKILL.md "Returning an applicable suggestion". */
export interface AppliedSuggestion {
  applies_to:
    | "idea"
    | "domain"
    | "name"
    | "dataset_name"
    | "description"
    | "purpose"
    | "schema_add_columns"
    | "schema_pick_columns"
    | "rule_decisions"
    | "rule_create"
    | "column_transform_set"
    | "shape_set"
    | "osi_metric_create"
    | "osi_relationship_create"
    | "osi_ai_context_set"
    // Phase 5: chat assistant can draft release notes + hint change_kind
    // when the PO is editing a deployed product. The wizard maps these
    // straight onto the same revisionNotes / changeKindOverride state
    // that ImpactPreviewPanel writes to.
    | "revision_notes"
    | "change_kind";
  value?: string;
  rationale?: string;
  columns?: Array<{
    name: string;
    logical_type?: string;
    physical_type?: string;
    description?: string;
    primary_key?: boolean;
  }>;
  /** schema_pick_columns payload — names of catalog columns the agent
   *  recommends ticking. Names that don't exist in the current catalog
   *  are silently ignored by the wizard. */
  column_names?: string[];
  decisions?: Array<{
    rule_uri: string;
    action: "approve" | "reject";
    rationale?: string;
  }>;
  /** rule_create payload — brand-new rules the chat is proposing. The
   *  wizard mints rule_uris at persist-time; the chat never invents URIs. */
  rules?: Array<{
    column: string;
    rule_type: string;
    severity?: string;
    description?: string;
    params?: Record<string, unknown>;
  }>;
  /** column_transform_set payload — set a transform hint on a single product
   *  column. The chat does not author SQL; it sets the structured intent and
   *  the engineer's mapping stage resolves source columns. */
  column?: string;
  transform?: {
    kind: string;        // direct | cast | format | concat | split | substring
                         //   | case | arithmetic | lookup | literal | expression
                         //   | bucket | mask | hash
    inputs?: string[];   // expected source-column names (logical, not URIs)
    separator?: string;  // concat
    params?: Record<string, unknown>;
    decorators?: { standardization?: string[]; default_if_null?: string };
    expression?: string;
  };
  /** shape_set payload — dataset-level Shape step authoring. Partial-update
   *  semantics: only fields PRESENT on the payload are applied; absent keys
   *  leave the wizard state untouched. The chat mirrors the wizard form so
   *  the PO can author conversationally:
   *    grain_prose        — free-form "one row per X" description
   *    filter             — raw SQL WHERE predicate fragment
   *    scd_policy         — Either a bare type string ('latest_only' |
   *                         'snapshot' | 'scd2' | '') OR an object that
   *                         carries the SCD-2 sub-fields:
   *                           {type:'scd2', effective_column, expiration_column,
   *                            add_is_current?}
   *                         Object form is required when proposing SCD-2 so
   *                         the wizard can populate the per-column dropdowns.
   *    grouping_keys      — list of product column names → GROUP BY keys
   *    suppressed_columns — list of product column names dropped from view
   *                         (PK names are filtered out by the handler) */
  shape?: {
    grain_prose?: string;
    filter?: string;
    filter_intent?: string;
    scd_policy?: string | {
      type: string;
      effective_column?: string;
      expiration_column?: string;
      add_is_current?: boolean;
    };
    grouping_keys?: string[];
    suppressed_columns?: string[];
  };
  // ── OSI advisor payloads (data-product-osi-advisor skill).
  /** osi_metric_create */
  expression?: string;
  dialect?: string;
  /** osi_relationship_create */
  from_dataset?: string;
  to_dataset?: string;
  from_columns?: string[];
  to_columns?: string[];
  /** osi_ai_context_set */
  instructions?: string;
  synonyms?: string[];
  examples?: string[];
}

interface ToolEvent {
  tool?: string;
  id?: string;
  input?: Record<string, unknown> | string;
}

interface DisplayMessage {
  localId: string;
  role: "user" | "assistant";
  content: string;
  suggestions: AppliedSuggestion[];
  toolEvents: ToolEvent[];
  pending?: boolean;
  persistedId?: number;
  /** Wizard state captured at send-time for live user messages. Surfaced
   *  in the chat as a compact expandable chip so the PO can see exactly
   *  what context reached the assistant on this turn. Historical messages
   *  loaded from the server have no snapshot (not persisted server-side
   *  in v1) — the chip is simply omitted. */
  contextSnapshot?: Record<string, unknown> | null;
}

/** Streaming-state visual cues. Three keyframe animations injected via a
 *  `<style>` tag in the rendered aside so this component doesn't depend on
 *  a global stylesheet:
 *    pa-pulse   — header status dot (subtle pulse when streaming)
 *    pa-dot     — three-dot "Thinking" indicator inside the assistant bubble
 *    pa-shimmer — thin progress bar across the top of the transcript pane */
const PRODUCT_CHAT_KEYFRAMES = `
@keyframes pa-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.35; }
}
@keyframes pa-dot {
  0%, 80%, 100% { transform: scale(0.5); opacity: 0.35; }
  40% { transform: scale(1); opacity: 1; }
}
@keyframes pa-shimmer {
  0% { transform: translateX(-100%); }
  100% { transform: translateX(100%); }
}
`;

/** User-facing step labels for the context chip. Mirrors the canonical
 *  `STEP_LABELS` map in `NewProductWizard.tsx` — keep these in sync if a
 *  step is renumbered or renamed. Duplicated locally so this component
 *  doesn't pull in the wizard module. */
const WIZARD_STEP_LABELS: Record<number, string> = {
  1: "Describe & Choose Domain",
  2: "Shape",
  3: "Suggest candidate sources",
  4: "Shape the Schema",
  5: "Product Details",
  6: "Rule Coach",
  7: "Readiness Review",
  8: "Confirm candidate sources",
  9: "Submitted",
};

interface SessionInfo {
  id: number;
  owner_email: string;
  project_id: number | null;
  title: string;
  created_at: string | null;
  updated_at: string | null;
}

interface PersistedMessage {
  id: number;
  session_id: number;
  role: "user" | "assistant";
  content: string;
  tool_events: ToolEvent[];
  created_at: string | null;
}

interface Props {
  open: boolean;
  onClose: () => void;
  ownerEmail: string;
  projectId: number | null;
  request: GuideMeRequest | null;
  onApply?: (suggestion: AppliedSuggestion) => string | void | Promise<string | void>;
  /** Latest wizard state at send-time. Carries to the agent out-of-band
   *  so it doesn't clutter the transcript. */
  getContext?: () => Record<string, unknown> | null;
  /** Drawer header label. Defaults to the wizard assistant name; the template
   *  editor passes "Template Authoring Assistant". */
  title?: string;
}

const SUGGESTION_BLOCK_RE = /```suggestion\s*\n([\s\S]*?)\n```/g;

// The applies_to values the wizard can actually render + apply. A block with an
// UNKNOWN applies_to (a skill/LLM typo like "shape" instead of "shape_set") must
// NOT become a suggestion card: its payload flows to formatSuggestionPreview's
// default arm, and if `value` is an object React throws #31 ("objects are not
// valid as a React child") — blanking the whole wizard. Gate on the known set.
const KNOWN_APPLIES_TO: ReadonlySet<string> = new Set<AppliedSuggestion["applies_to"]>([
  "idea", "domain", "name", "dataset_name", "description", "purpose",
  "schema_add_columns", "schema_pick_columns", "rule_decisions", "rule_create",
  "column_transform_set", "shape_set", "osi_metric_create",
  "osi_relationship_create", "osi_ai_context_set", "revision_notes", "change_kind",
]);

function extractSuggestions(text: string): { content: string; suggestions: AppliedSuggestion[] } {
  const suggestions: AppliedSuggestion[] = [];
  const stripped = text.replace(SUGGESTION_BLOCK_RE, (_match, body: string) => {
    try {
      const parsed = JSON.parse(body.trim());
      if (
        parsed && typeof parsed === "object"
        && typeof parsed.applies_to === "string"
        && KNOWN_APPLIES_TO.has(parsed.applies_to)
      ) {
        suggestions.push(parsed as AppliedSuggestion);
      }
    } catch {
      return `\`\`\`\n${body}\n\`\`\``;
    }
    return "";
  });
  return { content: stripped.trim(), suggestions };
}

export default function ProductChatPanel({
  open,
  onClose,
  ownerEmail,
  projectId,
  request,
  onApply,
  getContext,
  title = "Product Authoring Assistant",
}: Props) {
  const { status, lastEvent, sendMessage, deleteSession } = useProductChatSocket(open ? ownerEmail : null);
  const { getInfo: getSkillInfo } = useSkillRegistry();
  const confirm = useConfirm();

  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<number | null>(null);
  const [messages, setMessages] = useState<DisplayMessage[]>([]);
  const [input, setInput] = useState("");
  const [attachments, setAttachments] = useState<AttachmentDraft[]>([]);
  const [appliedNotes, setAppliedNotes] = useState<Record<string, string>>({});
  const transcriptRef = useRef<HTMLDivElement | null>(null);

  // Resize
  const [width, setWidth] = useState<number>(() => {
    try {
      const saved = Number(window.localStorage.getItem(WIDTH_STORAGE_KEY));
      if (Number.isFinite(saved) && saved >= MIN_WIDTH && saved <= MAX_WIDTH) return saved;
    } catch { /* ignore */ }
    return DEFAULT_WIDTH;
  });
  const dragStartRef = useRef<{ startX: number; startWidth: number } | null>(null);

  const onResizeMouseDown = (e: React.MouseEvent) => {
    e.preventDefault();
    dragStartRef.current = { startX: e.clientX, startWidth: width };
    const handleMove = (ev: MouseEvent) => {
      const s = dragStartRef.current;
      if (!s) return;
      const next = Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, s.startWidth + (s.startX - ev.clientX)));
      setWidth(next);
    };
    const handleUp = () => {
      if (dragStartRef.current) {
        try {
          window.localStorage.setItem(WIDTH_STORAGE_KEY, String(width));
        } catch { /* ignore */ }
      }
      dragStartRef.current = null;
      window.removeEventListener("mousemove", handleMove);
      window.removeEventListener("mouseup", handleUp);
    };
    window.addEventListener("mousemove", handleMove);
    window.addEventListener("mouseup", handleUp);
  };

  useEffect(() => {
    try {
      window.localStorage.setItem(WIDTH_STORAGE_KEY, String(width));
    } catch { /* ignore */ }
  }, [width]);

  // ── Sessions ─────────────────────────────────────────────────────────

  const loadSessions = useCallback(async (): Promise<SessionInfo[]> => {
    if (!ownerEmail) return [];
    const res = await api.get("/api/chat/product/sessions", { params: { owner_email: ownerEmail } });
    const list = (res.data || []) as SessionInfo[];
    setSessions(list);
    return list;
  }, [ownerEmail]);

  const loadMessages = useCallback(async (sessionId: number) => {
    const res = await api.get(`/api/chat/product/sessions/${sessionId}/messages`);
    const items = (res.data || []) as PersistedMessage[];
    setMessages(items.map((m) => {
      const { content, suggestions } = m.role === "assistant" ? extractSuggestions(m.content) : { content: m.content, suggestions: [] };
      return {
        localId: `persisted-${m.id}`,
        role: m.role,
        content,
        suggestions,
        toolEvents: m.tool_events || [],
        persistedId: m.id,
      };
    }));
    setAppliedNotes({});
  }, []);

  // On open, load sessions and select the most recent (or none).
  useEffect(() => {
    if (!open) return;
    loadSessions().then((list) => {
      if (list.length > 0) {
        setActiveSessionId(list[0].id);
      } else {
        setActiveSessionId(null);
        setMessages([]);
      }
    });
  }, [open, loadSessions]);

  // Track the previously-active session so we can distinguish (a) the first
  // pin after the drawer opens — where we want to PRESERVE any Guide-me
  // prefill that was just dropped into the composer — from (b) an explicit
  // session switch via the dropdown, where the composer is implicitly tied
  // to the prior session and should be cleared so the user starts blank.
  const prevSessionIdRef = useRef<number | null>(null);
  useEffect(() => {
    if (activeSessionId !== null) {
      loadMessages(activeSessionId);
    } else {
      setMessages([]);
    }
    const prev = prevSessionIdRef.current;
    if (prev !== null && activeSessionId !== null && prev !== activeSessionId) {
      // True switch between two existing sessions — drop any composer state
      // that belonged to the prior session.
      setInput("");
      setAttachments([]);
    }
    prevSessionIdRef.current = activeSessionId;
  }, [activeSessionId, loadMessages]);

  // ── Streaming events ─────────────────────────────────────────────────

  useEffect(() => {
    if (!lastEvent) return;
    const ev: ChatWSMessage = lastEvent;
    if (ev.type === "turn_started") {
      // Backend echoes the (possibly newly-created) session_id; pin it.
      if (ev.session_id) setActiveSessionId(ev.session_id);
      return;
    }
    if (ev.type === "text_delta" && ev.text) {
      setMessages((prev) => {
        const last = prev[prev.length - 1];
        if (last && last.role === "assistant" && last.pending) {
          return [...prev.slice(0, -1), { ...last, content: last.content + (ev.text || "") }];
        }
        return [...prev, {
          localId: `live-${Date.now()}`,
          role: "assistant",
          content: ev.text || "",
          suggestions: [],
          toolEvents: [],
          pending: true,
        }];
      });
      return;
    }
    if (ev.type === "tool_use") {
      const toolEvent: ToolEvent = { tool: ev.tool, id: ev.id, input: ev.input };
      setMessages((prev) => {
        const last = prev[prev.length - 1];
        if (last && last.role === "assistant" && last.pending) {
          return [...prev.slice(0, -1), { ...last, toolEvents: [...last.toolEvents, toolEvent] }];
        }
        return [...prev, {
          localId: `live-${Date.now()}`,
          role: "assistant",
          content: "",
          suggestions: [],
          toolEvents: [toolEvent],
          pending: true,
        }];
      });
      return;
    }
    if (ev.type === "turn_complete") {
      setMessages((prev) => prev.map((m) => {
        if (!m.pending) return m;
        const { content, suggestions } = extractSuggestions(m.content);
        return { ...m, content, suggestions, pending: false };
      }));
      // Refresh session metadata (title may have updated).
      loadSessions();
      return;
    }
    if (ev.type === "error") {
      setMessages((prev) => [...prev, {
        localId: `err-${Date.now()}`,
        role: "assistant",
        content: `⚠️ ${ev.message || "Error"}`,
        suggestions: [],
        toolEvents: [],
      }]);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lastEvent]);

  // Pre-fill from Guide-me click. Doesn't auto-create a session — keeps
  // active session in place, just drops the prefill into the composer.
  //
  // Auto-send fires EXACTLY ONCE per request (keyed on `nonce`) and only when
  // the socket is idle-connected. `status` is a dependency so a request that
  // arrives before the WS connects still fires once it does — but without the
  // nonce guard (and while it also accepted `streaming`) the effect re-ran on
  // every connected→streaming→connected transition and re-sent the same
  // prompt in a loop.
  const autoSentNonceRef = useRef<number | null>(null);
  useEffect(() => {
    if (!open || !request) return;
    setInput(request.prefill);
    if (
      request.autoSend
      && status === "connected"
      && request.nonce != null
      && autoSentNonceRef.current !== request.nonce
    ) {
      autoSentNonceRef.current = request.nonce;
      const ctx = getContext ? getContext() : request.context ?? null;
      const ok = sendMessage(request.prefill, projectId, ctx, activeSessionId);
      if (ok) {
        setMessages((prev) => [...prev, {
          localId: `live-user-${Date.now()}`,
          role: "user",
          content: request.prefill,
          suggestions: [],
          toolEvents: [],
          contextSnapshot: ctx,
        }]);
        setInput("");
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, request?.nonce, status]);

  useEffect(() => {
    transcriptRef.current?.scrollTo({ top: transcriptRef.current.scrollHeight });
  }, [messages]);

  const canSend = useMemo(
    () => (input.trim().length > 0 || attachments.length > 0) && status === "connected",
    [input, attachments.length, status]
  );

  // Header session badge — gives the PO an at-a-glance read on which session
  // is loaded after a switch (the symptom they reported was uncertainty about
  // whether a freshly opened panel was using current wizard state).
  const activeSession = useMemo(
    () => sessions.find((s) => s.id === activeSessionId) ?? null,
    [sessions, activeSessionId]
  );
  const activeSessionLabel = useMemo(() => {
    if (!activeSession) return "New session";
    const title = (activeSession.title || "").trim() || "New session";
    return title.length > 38 ? title.slice(0, 35) + "…" : title;
  }, [activeSession]);
  const userTurnCount = useMemo(
    () => messages.filter((m) => m.role === "user").length,
    [messages]
  );

  const handleSend = () => {
    if (!input.trim() && attachments.length === 0) return;
    if (status !== "connected") return;
    const text = input.trim();
    const ctx = getContext ? getContext() : null;
    const ok = sendMessage(text, projectId, ctx, activeSessionId, attachments);
    if (!ok) return;
    const visibleText = text || (attachments.length > 0 ? `📎 ${attachments.length} attachment(s)` : "");
    setMessages((prev) => [...prev, {
      localId: `live-user-${Date.now()}`,
      role: "user",
      content: visibleText,
      suggestions: [],
      toolEvents: [],
      contextSnapshot: ctx,
    }]);
    setInput("");
    setAttachments([]);
  };

  const handleNewSession = useCallback(async () => {
    const res = await api.post("/api/chat/product/sessions", {
      owner_email: ownerEmail,
      project_id: projectId,
    });
    const newSession = res.data as SessionInfo;
    setSessions((prev) => [newSession, ...prev]);
    setActiveSessionId(newSession.id);
    setMessages([]);
    setAppliedNotes({});
    // "+ New" is an explicit fresh-start gesture — clear any stale Guide-me
    // prefill or pending attachments left over from the prior session. The
    // active-session-change effect would also clear when both prev and new
    // are non-null, but doing it here too covers the first-session case
    // (null → N) where the effect's ref guard intentionally skips.
    setInput("");
    setAttachments([]);
  }, [ownerEmail, projectId]);

  const handleDeleteSession = useCallback(async () => {
    if (!activeSessionId) return;
    const ok = await confirm({
      title: "Delete chat session",
      message: "Delete this chat session and its history?",
      confirmLabel: "Delete",
      tone: "danger",
    });
    if (!ok) return;
    deleteSession(activeSessionId);
    try {
      await api.delete(`/api/chat/product/sessions/${activeSessionId}`);
    } catch { /* WS may have already deleted it */ }
    const next = sessions.filter((s) => s.id !== activeSessionId);
    setSessions(next);
    setActiveSessionId(next[0]?.id ?? null);
    setMessages([]);
  }, [activeSessionId, deleteSession, sessions, confirm]);

  const applySuggestion = async (key: string, s: AppliedSuggestion) => {
    if (!onApply) return;
    setAppliedNotes((prev) => ({ ...prev, [key]: "Applying..." }));
    try {
      const note = await onApply(s);
      setAppliedNotes((prev) => ({ ...prev, [key]: typeof note === "string" && note ? note : "Applied" }));
    } catch (e) {
      setAppliedNotes((prev) => ({ ...prev, [key]: e instanceof Error ? `Failed: ${e.message}` : "Failed" }));
    }
  };

  if (!open) return null;

  return (
    <aside
      style={{
        position: "fixed",
        right: 0,
        top: 0,
        bottom: 0,
        width,
        backgroundColor: "#fff",
        borderLeft: `1px solid ${productTheme.accent}`,
        display: "flex",
        flexDirection: "column",
        boxShadow: "-4px 0 12px rgba(0,0,0,0.08)",
        zIndex: 50,
      }}
    >
      <div
        onMouseDown={onResizeMouseDown}
        style={{
          position: "absolute",
          left: -3,
          top: 0,
          bottom: 0,
          width: 6,
          cursor: "ew-resize",
          zIndex: 1,
          backgroundColor: "transparent",
        }}
        title="Drag to resize"
      />

      {/* Keyframes for the streaming indicators. Inlined so this component
       *  doesn't depend on a global stylesheet. */}
      <style>{PRODUCT_CHAT_KEYFRAMES}</style>

      <header
        style={{
          padding: "12px 16px",
          borderBottom: "1px solid #e2e8f0",
          backgroundColor: productTheme.accentSoft,
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 8,
        }}
      >
        <div style={{ minWidth: 0, flex: 1 }}>
          <div style={{ fontWeight: 700, color: productTheme.accent, fontSize: 13 }}>{title}</div>
          <div style={{ fontSize: 11, color: "#64748b", display: "flex", alignItems: "center", gap: 6 }}>
            <span style={{
              display: "inline-block",
              width: 6,
              height: 6,
              borderRadius: "50%",
              background:
                status === "streaming" ? "#f59e0b"
                : status === "connected" ? "#22c55e"
                : "#94a3b8",
              animation: status === "streaming" ? "pa-pulse 1.2s ease-in-out infinite" : undefined,
              flexShrink: 0,
            }} />
            <span style={{
              whiteSpace: "nowrap",
              overflow: "hidden",
              textOverflow: "ellipsis",
            }}>
              {status === "streaming" ? "Thinking…" : status === "connected" ? "Ready" : "Disconnected"}
              {" · "}
              {activeSessionId === null
                ? "New session"
                : `${activeSessionLabel} (${userTurnCount} turn${userTurnCount === 1 ? "" : "s"})`}
            </span>
          </div>
        </div>
        <button type="button" onClick={onClose} style={headerButtonStyle()} title="Close">
          Close
        </button>
      </header>

      {/* Session bar */}
      <div
        style={{
          padding: "8px 14px",
          borderBottom: "1px solid #e2e8f0",
          display: "flex",
          alignItems: "center",
          gap: 8,
          background: "#f8fafc",
          fontSize: 12,
        }}
      >
        <select
          value={activeSessionId ?? ""}
          onChange={(e) => setActiveSessionId(e.target.value ? Number(e.target.value) : null)}
          style={{
            flex: 1,
            padding: "4px 6px",
            border: "1px solid #cbd5e1",
            borderRadius: 4,
            fontSize: 12,
            background: "white",
          }}
        >
          {sessions.length === 0 && <option value="">No sessions yet</option>}
          {sessions.map((s) => (
            <option key={s.id} value={s.id}>
              {sessionLabel(s)}
            </option>
          ))}
        </select>
        <button type="button" onClick={handleNewSession} style={smallSessionBtn()}>+ New</button>
        <button
          type="button"
          onClick={handleDeleteSession}
          disabled={activeSessionId === null}
          style={smallSessionBtn(activeSessionId === null)}
          title="Delete this session"
        >
          Delete
        </button>
      </div>

      {/* Shimmer progress bar — fires only while streaming. Sits above the
       *  transcript so it's visible even when the user has scrolled the
       *  conversation. */}
      <div
        aria-hidden
        style={{
          height: 2,
          background: status === "streaming" ? "#e2e8f0" : "transparent",
          overflow: "hidden",
          position: "relative",
        }}
      >
        {status === "streaming" && (
          <div
            style={{
              position: "absolute",
              inset: 0,
              background: `linear-gradient(90deg, transparent, ${productTheme.accent}, transparent)`,
              animation: "pa-shimmer 1.4s linear infinite",
            }}
          />
        )}
      </div>

      {/* Transcript */}
      <div
        ref={transcriptRef}
        style={{ flex: 1, overflowY: "auto", padding: "12px 14px", backgroundColor: "#f8fafc" }}
      >
        {messages.length === 0 && (
          <div style={{ color: "#94a3b8", fontSize: 13, textAlign: "center", padding: "40px 12px" }}>
            Click a <strong>Guide me</strong> button next to a wizard field, or type a question below.
          </div>
        )}
        {messages.map((m, idx) => (
          <MessageBubble
            key={m.localId}
            message={m}
            messageIndex={idx}
            getSkillInfo={getSkillInfo}
            onApply={onApply ? applySuggestion : undefined}
            appliedNotes={appliedNotes}
          />
        ))}
      </div>

      {/* Composer — inline textarea + Send button */}
      <div style={{ borderTop: "1px solid #e2e8f0", padding: "8px 14px", background: "#fff" }}>
        <div style={{ marginBottom: 6 }}>
          <AttachmentBar
            attachments={attachments}
            setAttachments={setAttachments}
            disabled={status === "disconnected" || status === "streaming"}
            accent={productTheme.accent}
          />
        </div>
        <div style={{ display: "flex", gap: 6 }}>
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                handleSend();
              }
            }}
            placeholder={status === "streaming" ? "Assistant is replying…" : "Ask for guidance…"}
            disabled={status === "disconnected" || status === "streaming"}
            style={{
              flex: 1,
              padding: "6px 8px",
              border: "1px solid #cbd5e1",
              borderRadius: 4,
              fontSize: 13,
              fontFamily: "inherit",
              resize: "vertical",
              minHeight: 80,
              maxHeight: 320,
              background: status === "streaming" ? "#f8fafc" : "#fff",
              cursor: status === "streaming" ? "not-allowed" : "text",
            }}
          />
          <button
            type="button"
            onClick={handleSend}
            disabled={!canSend}
            style={{
              padding: "6px 12px",
              backgroundColor: canSend ? productTheme.accent : "#94a3b8",
              color: "#fff",
              border: "none",
              borderRadius: 4,
              fontSize: 13,
              fontWeight: 500,
              cursor: canSend ? "pointer" : "not-allowed",
            }}
          >
            Send
          </button>
        </div>
      </div>
    </aside>
  );
}

function sessionLabel(s: SessionInfo): string {
  const title = (s.title || "").trim() || "New session";
  return title.length > 40 ? title.slice(0, 37) + "…" : title;
}

function headerButtonStyle(): React.CSSProperties {
  return {
    border: "1px solid #cbd5e1",
    backgroundColor: "#fff",
    borderRadius: 6,
    padding: "4px 10px",
    fontSize: 12,
    color: "#334155",
    cursor: "pointer",
  };
}

function smallSessionBtn(disabled = false): React.CSSProperties {
  return {
    padding: "4px 8px",
    border: "1px solid #cbd5e1",
    borderRadius: 4,
    fontSize: 12,
    background: "white",
    color: disabled ? "#cbd5e1" : "#334155",
    cursor: disabled ? "not-allowed" : "pointer",
  };
}

interface MessageBubbleProps {
  message: DisplayMessage;
  messageIndex: number;
  getSkillInfo: (name: string) => { slug: string; name: string; description: string } | null;
  onApply?: (key: string, s: AppliedSuggestion) => void;
  appliedNotes: Record<string, string>;
}

function MessageBubble({ message, messageIndex, getSkillInfo, onApply, appliedNotes }: MessageBubbleProps) {
  const isUser = message.role === "user";
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6, margin: "6px 0" }}>
      <div
        style={{
          alignSelf: isUser ? "flex-end" : "flex-start",
          maxWidth: "92%",
          padding: "8px 12px",
          backgroundColor: isUser ? productTheme.accent : "#fff",
          color: isUser ? "#fff" : "#0f172a",
          border: isUser ? "none" : "1px solid #e2e8f0",
          borderRadius: 10,
          fontSize: 13,
          lineHeight: 1.5,
        }}
      >
        {message.toolEvents.length > 0 && !isUser && (
          <div style={{ marginBottom: 4 }}>
            {message.toolEvents.map((ev, i) => {
              const lbl = formatToolUse(ev.tool, ev.input, getSkillInfo);
              return (
                <div
                  key={`${message.localId}-tool-${i}`}
                  style={{
                    margin: "4px 0",
                    padding: "4px 8px",
                    background: "#f1f5f9",
                    borderLeft: "3px solid #64748b",
                    fontSize: 11,
                    color: "#475569",
                    fontFamily: "monospace",
                    borderRadius: 3,
                    wordBreak: "break-all",
                  }}
                >
                  <strong>{lbl.label}</strong>
                  {lbl.description && (
                    <div style={{ fontSize: 10, color: "#64748b", marginTop: 2, fontFamily: "system-ui, sans-serif", fontWeight: 400 }}>
                      {lbl.description}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
        {isUser ? (
          <div style={{ whiteSpace: "pre-wrap" }}>{message.content}</div>
        ) : (
          <>
            {message.content && <MarkdownMessage content={message.content} />}
            {message.pending && <ThinkingDots />}
          </>
        )}
      </div>

      {/* Context chip — captured wizard-state snapshot at send-time. Only
       *  rendered for live user messages; historical (server-loaded) turns
       *  have no snapshot so this is silently omitted. */}
      {isUser && message.contextSnapshot && (
        <div style={{ alignSelf: "flex-end", maxWidth: "92%" }}>
          <ContextChip snapshot={message.contextSnapshot} />
        </div>
      )}

      {/* Apply cards rendered below the message bubble. Product-only. */}
      {message.suggestions.map((s, i) => {
        const key = `${messageIndex}:${i}`;
        const applied = appliedNotes[key];
        return (
          <SuggestionCard
            key={key}
            suggestion={s}
            applied={applied}
            onApply={onApply ? () => onApply(key, s) : undefined}
          />
        );
      })}
    </div>
  );
}

/** Three-dot "thinking" indicator for the in-progress assistant bubble.
 *  More visible than the previous `▍` cursor + grey "Thinking…" label,
 *  while still living inside the streaming bubble so it sits next to any
 *  partial content that has already arrived. */
function ThinkingDots() {
  const dotStyle: React.CSSProperties = {
    display: "inline-block",
    width: 6,
    height: 6,
    borderRadius: "50%",
    background: "#64748b",
    animation: "pa-dot 1.2s ease-in-out infinite",
  };
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 4, marginTop: 2 }}>
      <span style={{ ...dotStyle, animationDelay: "0s" }} />
      <span style={{ ...dotStyle, animationDelay: "0.15s" }} />
      <span style={{ ...dotStyle, animationDelay: "0.3s" }} />
    </span>
  );
}

/** Compact, click-to-expand summary of the wizard state that was attached
 *  to a given user message. Addresses the PO's concern that after a session
 *  switch they have no UI signal showing which wizard state reached the
 *  assistant. Renders only for live (in-session-just-sent) messages — see
 *  the limitation note on `DisplayMessage.contextSnapshot`. */
function ContextChip({ snapshot }: { snapshot: Record<string, unknown> }) {
  const [expanded, setExpanded] = useState(false);
  const { stepLabel, nonEmptyCount, entries } = useMemo(() => summarizeContext(snapshot), [snapshot]);
  return (
    <div
      style={{
        marginTop: 2,
        background: "#f1f5f9",
        borderLeft: "3px solid #94a3b8",
        borderRadius: 3,
        fontSize: 11,
        color: "#475569",
      }}
    >
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        style={{
          width: "100%",
          textAlign: "left",
          background: "transparent",
          border: "none",
          padding: "4px 8px",
          cursor: "pointer",
          color: "inherit",
          fontSize: "inherit",
          fontFamily: "inherit",
          display: "flex",
          alignItems: "center",
          gap: 6,
        }}
        title="Toggle context details"
      >
        <span aria-hidden>📋</span>
        <span style={{ flex: 1 }}>
          Context: {stepLabel} · {nonEmptyCount} field{nonEmptyCount === 1 ? "" : "s"}
        </span>
        <span style={{ fontSize: 10 }}>{expanded ? "▴" : "▾"}</span>
      </button>
      {expanded && (
        <div style={{
          padding: "4px 8px 6px 8px",
          fontFamily: "monospace",
          fontSize: 10,
          lineHeight: 1.4,
          wordBreak: "break-word",
          borderTop: "1px solid #e2e8f0",
        }}>
          {entries.length === 0 && <span style={{ color: "#94a3b8" }}>(no context captured)</span>}
          {entries.map(({ key, display }) => (
            <div key={key} style={{ margin: "2px 0" }}>
              <strong style={{ color: "#334155" }}>{key}:</strong> {display}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/** Turn a context-snapshot object into a step label + non-empty field
 *  count + a flat list of `(key, display-string)` pairs suitable for the
 *  expanded view. Strings >80 chars are truncated; arrays/objects are
 *  summarised by length or key count. Skips the `step` key from the
 *  detail rows since it's already in the header line. */
function summarizeContext(snapshot: Record<string, unknown>): {
  stepLabel: string;
  nonEmptyCount: number;
  entries: Array<{ key: string; display: string }>;
} {
  const stepRaw = snapshot.step;
  const stepNum = typeof stepRaw === "number" ? stepRaw : Number(stepRaw);
  const stepLabel = Number.isFinite(stepNum) && WIZARD_STEP_LABELS[stepNum]
    ? `Step ${stepNum} (${WIZARD_STEP_LABELS[stepNum]})`
    : "Step ?";

  const entries: Array<{ key: string; display: string }> = [];
  let nonEmptyCount = 0;
  for (const [key, value] of Object.entries(snapshot)) {
    if (key === "step") continue;
    const display = formatContextValue(value);
    if (!isContextValueEmpty(value)) nonEmptyCount += 1;
    entries.push({ key, display: display || "(empty)" });
  }
  return { stepLabel, nonEmptyCount, entries };
}

function isContextValueEmpty(value: unknown): boolean {
  if (value === null || value === undefined) return true;
  if (typeof value === "string") return value.trim().length === 0;
  if (typeof value === "boolean") return value === false;
  if (typeof value === "number") return false;
  if (Array.isArray(value)) return value.length === 0 || value.every(isContextValueEmpty);
  if (typeof value === "object") {
    return Object.values(value as object).every(isContextValueEmpty);
  }
  return false;
}

function formatContextValue(value: unknown): string {
  if (value === null || value === undefined) return "(empty)";
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (!trimmed) return "(empty)";
    return trimmed.length > 80 ? `"${trimmed.slice(0, 77)}…"` : `"${trimmed}"`;
  }
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) {
    if (value.length === 0) return "[0 items]";
    if (value.length <= 4 && value.every((v) => typeof v === "string")) {
      return `[${(value as string[]).map((s) => JSON.stringify(s)).join(", ")}]`;
    }
    return `[${value.length} items]`;
  }
  if (typeof value === "object") {
    const obj = value as Record<string, unknown>;
    const keys = Object.keys(obj);
    if (keys.length === 0) return "{0 keys}";
    // For small nested objects (like `shape`), inline a compact summary so
    // the PO can see at a glance whether shape fields are populated.
    const populated = keys.filter((k) => {
      const v = obj[k];
      if (v === null || v === undefined) return false;
      if (typeof v === "string") return v.trim().length > 0;
      if (Array.isArray(v)) return v.length > 0;
      if (typeof v === "object") return Object.keys(v as object).length > 0;
      return true;
    });
    return `{${populated.length}/${keys.length} populated: ${populated.slice(0, 4).join(", ")}${populated.length > 4 ? "…" : ""}}`;
  }
  return String(value);
}

function SuggestionCard({
  suggestion,
  applied,
  onApply,
}: {
  suggestion: AppliedSuggestion;
  applied?: string;
  onApply?: () => void;
}) {
  const label = fieldLabel(suggestion.applies_to);
  const preview = formatSuggestionPreview(suggestion);
  return (
    <div
      style={{
        alignSelf: "flex-start",
        maxWidth: "92%",
        border: `1px solid ${productTheme.accent}`,
        borderRadius: 10,
        backgroundColor: "#faf9fb",
        padding: 10,
        display: "flex",
        flexDirection: "column",
        gap: 6,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
        <div style={{ fontSize: 11, fontWeight: 700, letterSpacing: 0.4, textTransform: "uppercase", color: productTheme.accent }}>
          Suggestion → {label}
        </div>
        {applied ? (
          <span style={{ fontSize: 11, fontWeight: 600, color: "#059669" }}>✓ {applied}</span>
        ) : onApply ? (
          <button
            type="button"
            onClick={onApply}
            style={{
              padding: "4px 12px",
              borderRadius: 6,
              border: "none",
              backgroundColor: productTheme.accent,
              color: "#fff",
              fontSize: 12,
              fontWeight: 700,
              cursor: "pointer",
            }}
          >
            Apply
          </button>
        ) : null}
      </div>
      <div style={{ fontSize: 13, color: "#0f172a", whiteSpace: "pre-wrap" }}>{preview}</div>
      {typeof suggestion.rationale === "string" && suggestion.rationale && (
        <div style={{ fontSize: 11, color: "#64748b", fontStyle: "italic" }}>{suggestion.rationale}</div>
      )}
    </div>
  );
}

function fieldLabel(appliesTo: AppliedSuggestion["applies_to"]): string {
  switch (appliesTo) {
    case "idea": return "Product idea";
    case "domain": return "Domain";
    case "name": return "Product name";
    case "dataset_name": return "Dataset physical name";
    case "description": return "Description";
    case "purpose": return "Purpose";
    case "schema_add_columns": return "Add columns";
    case "schema_pick_columns": return "Pick catalog columns";
    case "rule_decisions": return "Rule decisions";
    case "rule_create": return "Add quality rules";
    case "column_transform_set": return "Set column derivation";
    case "shape_set": return "Shape dataset";
    case "osi_metric_create": return "Add OSI metric";
    case "osi_relationship_create": return "Add OSI relationship";
    case "osi_ai_context_set": return "Set AI context";
    case "revision_notes": return "Revision notes";
    case "change_kind": return "Change kind";
    default: return "Suggestion";
  }
}

function formatSuggestionPreview(s: AppliedSuggestion): string {
  if (s.applies_to === "schema_add_columns") {
    return (s.columns || [])
      .map((c) => `• ${c.name}${c.primary_key ? " (PK)" : ""} — ${c.physical_type || c.logical_type || "?"}`)
      .join("\n");
  }
  if (s.applies_to === "schema_pick_columns") {
    const names = s.column_names || [];
    return names.map((n) => `• ${n}`).join("\n") || "(no columns)";
  }
  if (s.applies_to === "rule_decisions") {
    return (s.decisions || [])
      .map((d) => `${d.action === "approve" ? "✓" : "✗"} ${d.rule_uri.split(":").slice(-2).join(":")}`)
      .join("\n");
  }
  if (s.applies_to === "rule_create") {
    return (s.rules || [])
      .map((r) => `• ${r.column}: ${r.rule_type}${r.description ? ` — ${r.description}` : ""}`)
      .join("\n");
  }
  if (s.applies_to === "column_transform_set") {
    const col = s.column || "<column>";
    const kind = s.transform?.kind || "<kind>";
    const inputs = (s.transform?.inputs || []).join(", ");
    const detail = inputs ? ` from (${inputs})` : "";
    return `${col} → ${kind}${detail}`;
  }
  if (s.applies_to === "shape_set") {
    const sh = s.shape || {};
    const lines: string[] = [];
    if (sh.grain_prose) lines.push(`Grain: ${sh.grain_prose}`);
    if (sh.filter) lines.push(`Filter: ${sh.filter}`);
    if (sh.scd_policy) {
      if (typeof sh.scd_policy === "string") {
        lines.push(`SCD policy: ${sh.scd_policy}`);
      } else {
        const scdParts: string[] = [`type=${sh.scd_policy.type}`];
        if (sh.scd_policy.effective_column) scdParts.push(`effective=${sh.scd_policy.effective_column}`);
        if (sh.scd_policy.expiration_column) scdParts.push(`expiration=${sh.scd_policy.expiration_column}`);
        if (sh.scd_policy.add_is_current) scdParts.push(`add_is_current=true`);
        lines.push(`SCD policy: ${scdParts.join(", ")}`);
      }
    }
    if (sh.grouping_keys?.length) lines.push(`Group by: ${sh.grouping_keys.join(", ")}`);
    if (sh.suppressed_columns?.length) lines.push(`Suppress: ${sh.suppressed_columns.join(", ")}`);
    return lines.join("\n") || "(no shape fields set)";
  }
  if (s.applies_to === "osi_metric_create") {
    const dialect = s.dialect ? ` [${s.dialect}]` : "";
    const desc = s.value ? ` — ${s.value}` : (s.rationale ? ` — ${s.rationale}` : "");
    return `${s.expression || ""}${dialect}${desc}`;
  }
  if (s.applies_to === "osi_relationship_create") {
    const fromCols = (s.from_columns || []).join(", ");
    const toCols = (s.to_columns || []).join(", ");
    return `${s.from_dataset}.(${fromCols}) → ${s.to_dataset}.(${toCols})`;
  }
  if (s.applies_to === "osi_ai_context_set") {
    const parts: string[] = [];
    if (s.instructions) parts.push(`Instructions: ${s.instructions}`);
    if (s.synonyms?.length) parts.push(`Synonyms: ${s.synonyms.join(", ")}`);
    if (s.examples?.length) parts.push(`Examples: ${s.examples.join(" / ")}`);
    return parts.join("\n");
  }
  // Defensive: `value` is typed string but arrives from JSON.parse of LLM
  // output — coerce so a stray object can never become a React child (#31).
  return typeof s.value === "string" ? s.value : "";
}
