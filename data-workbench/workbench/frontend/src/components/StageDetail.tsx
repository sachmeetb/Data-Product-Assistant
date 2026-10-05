import { useEffect, useMemo, useRef, useState } from "react";
import type { StageExecutionSummary, WSMessage } from "../types";
import AgentQuestionRenderer from "./AgentQuestionRenderer";
import RunSelector from "./RunSelector";
import StageOutputHeader from "./StageOutputHeader";
import MarkdownMessage from "./chat/MarkdownMessage";

interface Props {
  events: WSMessage[];
  stageName: string;
  pendingQuestions: WSMessage[];
  onAgentResponse: (questionId: string, value: string) => void;
  status: "running" | "complete" | "failed" | "awaiting_review" | "pending" | string;
  startedAt: string | null;
  completedAt: string | null;
  costUsd: number | null;
  truncated?: boolean;
  executions: StageExecutionSummary[];
  liveRunIds: Set<string>;
  selectedExecutionId: number | null;
  liveSelected: boolean;
  onSelectLive: () => void;
  onSelectExecution: (id: number) => void;
  isStreaming: boolean;
}

export default function StageDetail({
  events,
  stageName,
  pendingQuestions,
  onAgentResponse,
  status,
  startedAt,
  completedAt,
  costUsd,
  truncated,
  executions,
  liveRunIds,
  selectedExecutionId,
  liveSelected,
  onSelectLive,
  onSelectExecution,
  isStreaming,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const [expandedTools, setExpandedTools] = useState<Set<string>>(new Set());
  // The raw agent transcript is opt-in — it's noise for the engineer driving the
  // UI (and invisible in the Claude Code path). Status/timings (StageOutputHeader)
  // and run history (RunSelector) stay visible; the monospace stream collapses
  // behind a toggle. Forced open when the agent is asking a question (the
  // answer UI lives inside the transcript), so interactivity is never hidden.
  const [showLog, setShowLog] = useState(false);
  const transcriptForced = pendingQuestions.length > 0;
  const transcriptVisible = showLog || transcriptForced;

  useEffect(() => {
    // Only auto-scroll when this is the live/streaming buffer.
    if (!isStreaming) return;
    if (containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight;
    }
  }, [events, pendingQuestions, isStreaming]);

  const toggleTool = (id: string) => {
    setExpandedTools((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const pendingIds = new Set(pendingQuestions.map((q) => q.question_id));

  // Derive header counts from events for the live view; for a historical view
  // the parent already has authoritative counts in the execution summary,
  // but computing here keeps the component self-contained.
  const { eventCount, toolCounts } = useMemo(() => {
    const counts: Record<string, number> = {};
    let evCount = 0;
    for (const e of events) {
      evCount += 1;
      if (e.type === "tool_use" && e.tool) {
        counts[e.tool] = (counts[e.tool] ?? 0) + 1;
      }
    }
    return { eventCount: evCount, toolCounts: counts };
  }, [events]);

  // The agent's own prose narration (streamed text), concatenated — the clean
  // "what happened" progress story shown above the opt-in tool log.
  const narration = useMemo(
    () => events.filter((e) => e.type === "text_delta").map((e) => e.text || "").join(""),
    [events],
  );

  const durationLabel = (() => {
    if (!startedAt || !completedAt) return null;
    const s = Math.max(0, Math.round((new Date(completedAt).getTime() - new Date(startedAt).getTime()) / 1000));
    return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
  })();
  const isComplete = status === "complete" || status === "awaiting_review";

  return (
    <div>
      <RunSelector
        executions={executions}
        liveRunIds={liveRunIds}
        selectedExecutionId={selectedExecutionId}
        liveSelected={liveSelected}
        onSelectLive={onSelectLive}
        onSelectExecution={onSelectExecution}
      />
      {/* Prominent activity summary line */}
      {events.length > 0 && isComplete && (
        <div
          style={{
            margin: "8px 0", padding: "9px 13px",
            backgroundColor: "#f0fdf4", border: "1px solid #bbf7d0", borderRadius: 8,
            fontSize: 13, fontWeight: 600, color: "#166534",
          }}
        >
          ✓ Complete{durationLabel ? ` · ${durationLabel}` : ""}{costUsd != null ? ` · $${costUsd.toFixed(4)}` : ""}
          <span style={{ fontWeight: 400, color: "#16a34a" }}>{` · ${eventCount} steps`}</span>
        </div>
      )}
      {/* Step progress — the agent's own narration, de-terminalized. Reliable
          because it's the agent's words, not parsed tool calls. Full tool
          transcript stays behind the opt-in log below. */}
      {narration.trim() && (
        <div
          style={{
            margin: "10px 0", padding: "4px 14px",
            backgroundColor: "#f8fafc", border: "1px solid #e2e8f0", borderRadius: 8,
            fontSize: 13, lineHeight: 1.6, color: "#334155",
            maxHeight: 320, overflow: "auto",
          }}
        >
          <MarkdownMessage content={narration.trim()} />
        </div>
      )}
      {!transcriptForced && events.length > 0 && (
        <button
          onClick={() => setShowLog((v) => !v)}
          style={{
            margin: "4px 0 8px", padding: "4px 10px", borderRadius: 6,
            border: "1px solid #cbd5e1", backgroundColor: "#fff", color: "#475569",
            fontSize: 12, fontWeight: 600, cursor: "pointer",
          }}
        >
          {showLog ? "▾ Hide agent log" : `▸ Show agent log (${eventCount} events)`}
        </button>
      )}
      {transcriptVisible && (
      <div
        ref={containerRef}
        style={{
          backgroundColor: "#1e293b",
          borderRadius: 8,
          padding: 16,
          color: "#e2e8f0",
          fontFamily: "monospace",
          fontSize: 13,
          lineHeight: 1.6,
          maxHeight: 500,
          overflow: "auto",
        }}
      >
        <div style={{ color: "#94a3b8", marginBottom: 8 }}>--- {stageName} ---</div>
        {events.length === 0 && (
          <div style={{ color: "#64748b", fontStyle: "italic" }}>
            No transcript yet. Run this stage to see streamed output here.
          </div>
        )}
        {events.map((msg, i) => {
          if (msg.type === "text_delta") {
            return (
              <span key={i} style={{ whiteSpace: "pre-wrap" }}>
                {msg.text}
              </span>
            );
          }
          if (msg.type === "thinking") {
            return (
              <div key={i} style={{ color: "#64748b", fontStyle: "italic" }}>
                {msg.text}
              </div>
            );
          }
          if (msg.type === "tool_use") {
            const toolId = msg.id || `tool-${i}`;
            const isExpanded = expandedTools.has(toolId);
            return (
              <div
                key={i}
                style={{
                  margin: "6px 0",
                  backgroundColor: "#0f172a",
                  borderRadius: 6,
                  padding: "6px 10px",
                  borderLeft: "3px solid #3b82f6",
                }}
              >
                <div
                  onClick={() => toggleTool(toolId)}
                  style={{ cursor: "pointer", color: "#60a5fa", fontWeight: 600 }}
                >
                  {isExpanded ? "▼" : "▶"} {msg.tool}
                </div>
                {isExpanded && (
                  <pre style={{ margin: "4px 0 0", color: "#94a3b8", fontSize: 12, overflow: "auto" }}>
                    {JSON.stringify(msg.input, null, 2)}
                  </pre>
                )}
              </div>
            );
          }
          if (msg.type === "agent_question") {
            const isPending = pendingIds.has(msg.question_id);
            if (isPending && isStreaming) {
              return (
                <AgentQuestionRenderer
                  key={`q-${msg.question_id}`}
                  question={msg}
                  onRespond={onAgentResponse}
                />
              );
            }
            return (
              <div
                key={`q-${msg.question_id}-${i}`}
                style={{
                  margin: "6px 0",
                  padding: "6px 10px",
                  borderRadius: 6,
                  borderLeft: "3px solid #22c55e",
                  backgroundColor: "#0f172a",
                  fontSize: 12,
                  color: "#94a3b8",
                }}
              >
                Agent asked: {msg.prompt}
              </div>
            );
          }
          if (msg.type === "agent_timeout") {
            return (
              <div
                key={`timeout-${msg.question_id}-${i}`}
                style={{
                  margin: "6px 0",
                  padding: "6px 10px",
                  borderRadius: 6,
                  borderLeft: "3px solid #f59e0b",
                  backgroundColor: "#0f172a",
                  fontSize: 12,
                  color: "#f59e0b",
                }}
              >
                Question timed out{msg.default_used ? " (using default)" : ""}
              </div>
            );
          }
          if (msg.type === "stage_complete") {
            return (
              <div key={i} style={{ color: "#22c55e", marginTop: 8, fontWeight: 600 }}>
                Stage complete{msg.cost_usd != null ? ` (cost: $${msg.cost_usd.toFixed(4)})` : ""}
              </div>
            );
          }
          if (msg.type === "error") {
            return (
              <div key={i} style={{ color: "#ef4444", marginTop: 8, fontWeight: 600 }}>
                Error: {msg.message}
              </div>
            );
          }
          if (msg.type === "stage_started") {
            return (
              <div key={i} style={{ color: "#f59e0b", fontWeight: 600 }}>
                Starting: {msg.stage_name}
              </div>
            );
          }
          if (msg.type === "log_truncated") {
            return (
              <div key={i} style={{ color: "#f59e0b", fontStyle: "italic", marginTop: 8 }}>
                [transcript truncated at {msg.at_event ?? "?"} events — older output not persisted]
              </div>
            );
          }
          return null;
        })}
        <div ref={bottomRef} />
      </div>
      )}
      {/* Detailed counts — moved to the bottom; the green line above is the headline. */}
      {events.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <StageOutputHeader
            status={status}
            startedAt={startedAt}
            completedAt={completedAt}
            costUsd={costUsd}
            eventCount={eventCount}
            toolCounts={toolCounts}
            truncated={truncated}
          />
        </div>
      )}
    </div>
  );
}
