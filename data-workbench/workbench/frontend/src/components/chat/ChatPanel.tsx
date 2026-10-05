import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import api from "../../api/client";
import type {
  ChatMessageInfo,
  ChatSessionInfo,
  ChatToolEvent,
  ChatWSMessage,
} from "../../types";
import { useChatSocket } from "../../hooks/useChatSocket";
import { useSkillRegistry } from "../../lib/skillRegistry";
import MarkdownMessage from "./MarkdownMessage";
import {
  promptsForContext,
  type ChatContextKey,
  type SuggestedPrompt,
} from "./suggestedPrompts";
import { formatToolUse } from "./toolUseLabel";
import AttachmentBar, { type AttachmentDraft } from "./AttachmentBar";

interface Props {
  projectId: number;
  projectCode: string;
  context: ChatContextKey;
  onClose: () => void;
  /**
   * Optional one-shot prefill for the input field. When the parent updates
   * this prop with a non-null value (typically from a "Guide me" affordance
   * in a child panel), the textarea is seeded with the text and the parent
   * is expected to clear the prefill via onPrefillConsumed so subsequent
   * opens don't re-seed stale text. The seed replaces any currently-typed
   * value because Guide-me is an explicit "ask about this thing" intent.
   */
  inputPrefill?: string | null;
  onPrefillConsumed?: () => void;
}

interface DisplayMessage {
  localId: string;
  role: "user" | "assistant";
  content: string;
  toolEvents: ChatToolEvent[];
  persistedId?: number;
  pending?: boolean;
}

const MIN_WIDTH = 320;
const DEFAULT_WIDTH = 400;
// Engineer chat keeps its own storage key — Product chat persists under
// "product_chat.drawerWidth" so each workbench remembers its width
// independently. Both panels share the resize behaviour below.
const WIDTH_STORAGE_KEY = "engineer_chat.drawerWidth";

function computeMaxWidth(): number {
  if (typeof window === "undefined") return 1200;
  return Math.min(window.innerWidth * 0.8, 1200);
}

function clampWidth(w: number): number {
  return Math.max(MIN_WIDTH, Math.min(w, computeMaxWidth()));
}

function loadInitialWidth(): number {
  try {
    const raw = localStorage.getItem(WIDTH_STORAGE_KEY);
    if (!raw) return DEFAULT_WIDTH;
    const parsed = parseInt(raw, 10);
    if (!Number.isFinite(parsed)) return DEFAULT_WIDTH;
    return clampWidth(parsed);
  } catch {
    return DEFAULT_WIDTH;
  }
}

const styles = {
  drawer: {
    position: "fixed" as const,
    top: 0,
    right: 0,
    height: "100vh",
    background: "#ffffff",
    borderLeft: "1px solid #e2e8f0",
    boxShadow: "-4px 0 16px rgba(15,23,42,0.08)",
    display: "flex",
    flexDirection: "column" as const,
    zIndex: 100,
  },
  header: {
    padding: "12px 14px",
    borderBottom: "1px solid #e2e8f0",
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    background: "#0f172a",
    color: "white",
  },
  title: {
    fontSize: 14,
    fontWeight: 600,
    margin: 0,
  },
  subtitle: {
    fontSize: 11,
    color: "#94a3b8",
    marginTop: 2,
  },
  closeBtn: {
    background: "transparent",
    color: "white",
    border: "1px solid #334155",
    padding: "4px 8px",
    borderRadius: 4,
    cursor: "pointer",
    fontSize: 12,
  },
  sessionBar: {
    padding: "8px 14px",
    borderBottom: "1px solid #e2e8f0",
    display: "flex",
    alignItems: "center",
    gap: 8,
    background: "#f8fafc",
    fontSize: 12,
  },
  select: {
    flex: 1,
    padding: "4px 6px",
    border: "1px solid #cbd5e1",
    borderRadius: 4,
    fontSize: 12,
    background: "white",
  },
  newBtn: {
    padding: "4px 8px",
    border: "1px solid #cbd5e1",
    borderRadius: 4,
    fontSize: 12,
    background: "white",
    cursor: "pointer",
  },
  messages: {
    flex: 1,
    overflowY: "auto" as const,
    padding: "12px 14px",
    background: "#f8fafc",
  },
  empty: {
    color: "#64748b",
    fontSize: 13,
    textAlign: "center" as const,
    padding: "40px 12px",
  },
  userBubble: {
    margin: "6px 0",
    padding: "8px 12px",
    background: "#2563eb",
    color: "white",
    borderRadius: 10,
    maxWidth: "85%",
    marginLeft: "15%",
    fontSize: 13,
    whiteSpace: "pre-wrap" as const,
  },
  assistantBubble: {
    margin: "6px 0",
    padding: "8px 12px",
    background: "white",
    color: "#0f172a",
    border: "1px solid #e2e8f0",
    borderRadius: 10,
    maxWidth: "90%",
    fontSize: 13,
  },
  toolEvent: {
    margin: "4px 0",
    padding: "4px 8px",
    background: "#f1f5f9",
    borderLeft: "3px solid #64748b",
    fontSize: 11,
    color: "#475569",
    fontFamily: "monospace",
    borderRadius: 3,
    whiteSpace: "pre-wrap" as const,
    wordBreak: "break-all" as const,
  },
  inputArea: {
    borderTop: "1px solid #e2e8f0",
    padding: "8px 14px",
    background: "white",
  },
  suggestionsRow: {
    display: "flex",
    flexWrap: "wrap" as const,
    gap: 6,
    marginBottom: 8,
  },
  chip: {
    padding: "4px 8px",
    border: "1px solid #cbd5e1",
    borderRadius: 999,
    fontSize: 11,
    background: "white",
    cursor: "pointer",
    color: "#334155",
  },
  inputRow: {
    display: "flex",
    gap: 6,
  },
  textarea: {
    flex: 1,
    padding: "6px 8px",
    border: "1px solid #cbd5e1",
    borderRadius: 4,
    fontSize: 13,
    fontFamily: "inherit",
    resize: "vertical" as const,
    minHeight: 80,
    maxHeight: 320,
  },
  sendBtn: {
    padding: "6px 12px",
    background: "#2563eb",
    color: "white",
    border: "none",
    borderRadius: 4,
    fontSize: 13,
    cursor: "pointer",
    fontWeight: 500,
  },
  sendBtnDisabled: {
    padding: "6px 12px",
    background: "#94a3b8",
    color: "white",
    border: "none",
    borderRadius: 4,
    fontSize: 13,
    cursor: "not-allowed",
    fontWeight: 500,
  },
};

export default function ChatPanel({
  projectId, projectCode, context, onClose,
  inputPrefill = null, onPrefillConsumed,
}: Props) {
  const [sessions, setSessions] = useState<ChatSessionInfo[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<number | null>(null);
  const [messages, setMessages] = useState<DisplayMessage[]>([]);
  const [input, setInput] = useState("");

  // Apply the prefill once on mount and again whenever the parent supplies
  // a new non-null value. Whoever set the prefill is responsible for
  // clearing it after we consume so the input doesn't keep re-seeding on
  // re-renders.
  useEffect(() => {
    if (inputPrefill && inputPrefill.length > 0) {
      setInput(inputPrefill);
      if (onPrefillConsumed) onPrefillConsumed();
    }
  }, [inputPrefill, onPrefillConsumed]);
  const [attachments, setAttachments] = useState<AttachmentDraft[]>([]);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState<number>(() => loadInitialWidth());
  const [isResizing, setIsResizing] = useState(false);
  const resizeStartRef = useRef<{ startX: number; startWidth: number } | null>(null);
  const { status, lastEvent, sendMessage } = useChatSocket(projectId);
  const { getInfo: getSkillInfo } = useSkillRegistry();

  useEffect(() => {
    try {
      localStorage.setItem(WIDTH_STORAGE_KEY, String(width));
    } catch {
      // ignore storage failures (private mode, quota, etc.)
    }
  }, [width]);

  useEffect(() => {
    try {
      localStorage.setItem(WIDTH_STORAGE_KEY, String(width));
    } catch {
      // ignore storage failures (private mode, quota, etc.)
    }
  }, [width]);

  useEffect(() => {
    if (!isResizing) return;
    const onMove = (e: MouseEvent) => {
      const start = resizeStartRef.current;
      if (!start) return;
      const next = start.startWidth + (start.startX - e.clientX);
      setWidth(clampWidth(next));
    };
    const onUp = () => {
      setIsResizing(false);
      resizeStartRef.current = null;
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
    const prevUserSelect = document.body.style.userSelect;
    const prevCursor = document.body.style.cursor;
    document.body.style.userSelect = "none";
    document.body.style.cursor = "col-resize";
    return () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      document.body.style.userSelect = prevUserSelect;
      document.body.style.cursor = prevCursor;
    };
  }, [isResizing]);

  const startResize = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      resizeStartRef.current = { startX: e.clientX, startWidth: width };
      setIsResizing(true);
    },
    [width]
  );

  const suggestions = useMemo<SuggestedPrompt[]>(
    () => promptsForContext(context),
    [context]
  );

  const loadSessions = useCallback(async () => {
    const res = await api.get(`/api/projects/${projectId}/chat/sessions`);
    setSessions(res.data);
    return res.data as ChatSessionInfo[];
  }, [projectId]);

  const loadMessages = useCallback(async (sessionId: number) => {
    const res = await api.get(`/api/chat/sessions/${sessionId}/messages`);
    const items = res.data as ChatMessageInfo[];
    setMessages(
      items.map((m) => ({
        localId: `persisted-${m.id}`,
        role: m.role,
        content: m.content,
        toolEvents: m.tool_events || [],
        persistedId: m.id,
      }))
    );
  }, []);

  useEffect(() => {
    loadSessions().then((ss) => {
      if (ss.length > 0) {
        setActiveSessionId(ss[0].id);
      }
    });
  }, [loadSessions]);

  useEffect(() => {
    if (activeSessionId !== null) {
      loadMessages(activeSessionId);
    } else {
      setMessages([]);
    }
  }, [activeSessionId, loadMessages]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  useEffect(() => {
    if (!lastEvent) return;
    const msg: ChatWSMessage = lastEvent;

    if (msg.type === "turn_started" && msg.session_id) {
      setActiveSessionId(msg.session_id);
      return;
    }

    if (msg.type === "text_delta" && msg.text) {
      setMessages((prev) => {
        const last = prev[prev.length - 1];
        if (last && last.role === "assistant" && last.pending) {
          return [
            ...prev.slice(0, -1),
            { ...last, content: last.content + msg.text },
          ];
        }
        return [
          ...prev,
          {
            localId: `live-${Date.now()}`,
            role: "assistant",
            content: msg.text || "",
            toolEvents: [],
            pending: true,
          },
        ];
      });
      return;
    }

    if (msg.type === "tool_use") {
      setMessages((prev) => {
        const last = prev[prev.length - 1];
        const toolEvent: ChatToolEvent = {
          type: "tool_use",
          tool: msg.tool,
          id: msg.id,
          input: msg.input,
        };
        if (last && last.role === "assistant" && last.pending) {
          return [
            ...prev.slice(0, -1),
            { ...last, toolEvents: [...last.toolEvents, toolEvent] },
          ];
        }
        return [
          ...prev,
          {
            localId: `live-${Date.now()}`,
            role: "assistant",
            content: "",
            toolEvents: [toolEvent],
            pending: true,
          },
        ];
      });
      return;
    }

    if (msg.type === "turn_complete") {
      setMessages((prev) =>
        prev.map((m) => (m.pending ? { ...m, pending: false } : m))
      );
      loadSessions();
      return;
    }

    if (msg.type === "error") {
      setMessages((prev) => [
        ...prev,
        {
          localId: `err-${Date.now()}`,
          role: "assistant",
          content: `Error: ${msg.message || "unknown"}`,
          toolEvents: [],
        },
      ]);
    }
  }, [lastEvent, loadSessions]);

  const handleSend = useCallback(() => {
    const trimmed = input.trim();
    if (!trimmed && attachments.length === 0) return;
    if (status !== "connected" && status !== "streaming") return;
    if (status === "streaming") return;

    const visibleText = trimmed || (attachments.length > 0 ? `📎 ${attachments.length} attachment(s)` : "");
    setMessages((prev) => [
      ...prev,
      {
        localId: `live-user-${Date.now()}`,
        role: "user",
        content: visibleText,
        toolEvents: [],
      },
    ]);
    sendMessage(trimmed, activeSessionId, attachments);
    setInput("");
    setAttachments([]);
  }, [input, attachments, status, sendMessage, activeSessionId]);

  const handleNewSession = useCallback(async () => {
    const res = await api.post(`/api/projects/${projectId}/chat/sessions`, {});
    const newSession = res.data as ChatSessionInfo;
    setSessions((prev) => [newSession, ...prev]);
    setActiveSessionId(newSession.id);
    setMessages([]);
  }, [projectId]);

  const sessionLabel = (s: ChatSessionInfo) => {
    const title = (s.title || "").trim() || "Untitled";
    return title.length > 40 ? title.slice(0, 37) + "..." : title;
  };

  return (
    <div style={{ ...styles.drawer, width }}>
      <div
        onMouseDown={startResize}
        style={{
          position: "absolute",
          top: 0,
          left: 0,
          bottom: 0,
          width: 6,
          cursor: "col-resize",
          background: isResizing ? "#cbd5e1" : "transparent",
          zIndex: 1,
        }}
        onMouseEnter={(e) => {
          if (!isResizing) e.currentTarget.style.background = "#e2e8f0";
        }}
        onMouseLeave={(e) => {
          if (!isResizing) e.currentTarget.style.background = "transparent";
        }}
        title="Drag to resize"
      />
      <div style={styles.header}>
        <div>
          <div style={styles.title}>Ask</div>
          <div style={styles.subtitle}>
            Project {projectCode} · {status}
          </div>
        </div>
        <button style={styles.closeBtn} onClick={onClose}>
          Close
        </button>
      </div>

      <div style={styles.sessionBar}>
        <select
          style={styles.select}
          value={activeSessionId ?? ""}
          onChange={(e) =>
            setActiveSessionId(e.target.value ? Number(e.target.value) : null)
          }
        >
          <option value="">New session</option>
          {sessions.map((s) => (
            <option key={s.id} value={s.id}>
              {sessionLabel(s)}
            </option>
          ))}
        </select>
        <button style={styles.newBtn} onClick={handleNewSession}>
          + New
        </button>
      </div>

      <div style={styles.messages}>
        {messages.length === 0 ? (
          <div style={styles.empty}>
            Ask a question about this project.
            <br />
            Try one of the suggestions below.
          </div>
        ) : (
          messages.map((m) => (
            <div key={m.localId}>
              {m.role === "user" ? (
                <div style={styles.userBubble}>{m.content}</div>
              ) : (
                <div style={styles.assistantBubble}>
                  {m.toolEvents.length > 0 && (
                    <div>
                      {m.toolEvents.map((ev, i) => {
                        const lbl = formatToolUse(ev.tool, ev.input, getSkillInfo);
                        return (
                          <div key={`${m.localId}-tool-${i}`} style={styles.toolEvent}>
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
                  {m.content && <MarkdownMessage content={m.content} />}
                  {m.pending && !m.content && m.toolEvents.length === 0 && (
                    <span style={{ color: "#94a3b8" }}>Thinking...</span>
                  )}
                </div>
              )}
            </div>
          ))
        )}
        <div ref={messagesEndRef} />
      </div>

      <div style={styles.inputArea}>
        {messages.length === 0 && suggestions.length > 0 && (
          <div style={styles.suggestionsRow}>
            {suggestions.map((s, i) => (
              <button
                key={i}
                style={styles.chip}
                onClick={() => setInput(s.prompt)}
                type="button"
              >
                {s.label}
              </button>
            ))}
          </div>
        )}
        <div style={{ marginBottom: 6 }}>
          <AttachmentBar
            attachments={attachments}
            setAttachments={setAttachments}
            disabled={status === "disconnected" || status === "streaming"}
            accent="#2563eb"
          />
        </div>
        <div style={styles.inputRow}>
          <textarea
            style={styles.textarea}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                handleSend();
              }
            }}
            placeholder="Ask about this project..."
            disabled={status === "disconnected"}
          />
          <button
            style={
              status === "streaming" || (!input.trim() && attachments.length === 0)
                ? styles.sendBtnDisabled
                : styles.sendBtn
            }
            onClick={handleSend}
            disabled={status === "streaming" || (!input.trim() && attachments.length === 0)}
          >
            Send
          </button>
        </div>
      </div>
    </div>
  );
}
