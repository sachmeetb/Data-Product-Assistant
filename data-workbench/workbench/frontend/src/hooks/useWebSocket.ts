import { useCallback, useEffect, useRef, useState } from "react";
import { wsUrl } from "../api/client";
import type { WSMessage } from "../types";

export interface LiveRun {
  workflowId: string | null;
  stageNumber: number;
  runId: string;
  startedAt: string;
  status: "running" | "complete" | "failed" | "awaiting_review";
  cost_usd: number | null;
}

const bufferKey = (workflowId: string | null | undefined, stageNumber: number, runId: string) =>
  `${workflowId ?? "-"}:${stageNumber}:${runId}`;

export function useWebSocket(projectId: number | null) {
  const [buffers, setBuffers] = useState<Map<string, WSMessage[]>>(new Map());
  const [runIndex, setRunIndex] = useState<Map<string, LiveRun>>(new Map());
  const [status, setStatus] = useState<"disconnected" | "connected" | "running">("disconnected");
  const [pendingQuestions, setPendingQuestions] = useState<WSMessage[]>([]);
  const [lastEvent, setLastEvent] = useState<WSMessage | null>(null);
  const runIdRef = useRef<string | null>(null);
  const currentKeyRef = useRef<string | null>(null);
  const currentRunMetaRef = useRef<{ workflowId: string | null; stageNumber: number } | null>(null);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    if (projectId === null) return;

    // Clear any state carried over from a previous project. Buffers/runIndex
    // are keyed by (workflowId, stageNumber, runId) — not by project — so
    // without this reset, switching between projects that happen to share a
    // stage slot would show the prior project's live output.
    setBuffers(new Map());
    setRunIndex(new Map());
    setPendingQuestions([]);
    setLastEvent(null);
    runIdRef.current = null;
    currentKeyRef.current = null;
    currentRunMetaRef.current = null;

    const ws = new WebSocket(wsUrl(`/ws/pipeline/${projectId}`));
    wsRef.current = ws;

    ws.onopen = () => setStatus("connected");
    ws.onclose = () => setStatus("disconnected");
    ws.onerror = () => setStatus("disconnected");

    ws.onmessage = (event) => {
      const msg: WSMessage = JSON.parse(event.data);
      setLastEvent(msg);

      if (msg.type === "stage_started" && msg.run_id && msg.stage_number != null) {
        const workflowId = msg.workflow_id ?? null;
        const key = bufferKey(workflowId, msg.stage_number, msg.run_id);
        currentKeyRef.current = key;
        currentRunMetaRef.current = { workflowId, stageNumber: msg.stage_number };
        runIdRef.current = msg.run_id;
        setBuffers((prev) => {
          const next = new Map(prev);
          next.set(key, [msg]);
          return next;
        });
        setRunIndex((prev) => {
          const next = new Map(prev);
          next.set(key, {
            workflowId,
            stageNumber: msg.stage_number!,
            runId: msg.run_id!,
            startedAt: msg.started_at ?? new Date().toISOString(),
            status: "running",
            cost_usd: null,
          });
          return next;
        });
        setStatus("running");
        return;
      }

      // All non-stage_started events append to the current run's buffer
      const key = currentKeyRef.current;
      if (key) {
        setBuffers((prev) => {
          const next = new Map(prev);
          const arr = next.get(key) ?? [];
          next.set(key, [...arr, msg]);
          return next;
        });
      }

      if (msg.type === "stage_complete" || msg.type === "error") {
        setStatus("connected");
        if (key) {
          setRunIndex((prev) => {
            const next = new Map(prev);
            const existing = next.get(key);
            if (existing) {
              next.set(key, {
                ...existing,
                status: msg.type === "error" ? "failed" : "complete",
                cost_usd: typeof msg.cost_usd === "number" ? msg.cost_usd : existing.cost_usd,
              });
            }
            return next;
          });
        }
        runIdRef.current = null;
        currentKeyRef.current = null;
        currentRunMetaRef.current = null;
        setPendingQuestions([]);
      }

      if (msg.type === "stage_status_changed" && key) {
        setRunIndex((prev) => {
          const next = new Map(prev);
          const existing = next.get(key);
          if (existing && msg.status) {
            next.set(key, { ...existing, status: msg.status as LiveRun["status"] });
          }
          return next;
        });
      }

      // Agent question handling
      if (msg.type === "agent_question") {
        setPendingQuestions((prev) => [...prev, msg]);
      }
      if (msg.type === "agent_timeout") {
        setPendingQuestions((prev) =>
          prev.filter((q) => q.question_id !== msg.question_id)
        );
      }
    };

    return () => {
      ws.close();
      wsRef.current = null;
    };
  }, [projectId]);

  const sendAction = useCallback(
    (action: string, payload: Record<string, unknown> = {}) => {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({ action, ...payload }));
      }
    },
    []
  );

  const sendAgentResponse = useCallback(
    (questionId: string, value: string) => {
      const runId = runIdRef.current;
      if (!runId) return;

      // Remove from pending
      setPendingQuestions((prev) =>
        prev.filter((q) => q.question_id !== questionId)
      );

      // Add a record of the response to the current run's buffer for display
      const key = currentKeyRef.current;
      if (key) {
        setBuffers((prev) => {
          const next = new Map(prev);
          const arr = next.get(key) ?? [];
          next.set(key, [
            ...arr,
            { type: "text_delta" as const, text: `\n[User responded: ${value}]\n` },
          ]);
          return next;
        });
      }

      sendAction("agent_response", {
        run_id: runId,
        question_id: questionId,
        value,
      });
    },
    [sendAction]
  );

  const getLiveRuns = useCallback(
    (workflowId: string | null | undefined, stageNumber: number): LiveRun[] => {
      const target = workflowId ?? null;
      return Array.from(runIndex.values())
        .filter((r) => r.stageNumber === stageNumber && r.workflowId === target)
        .sort((a, b) => b.startedAt.localeCompare(a.startedAt));
    },
    [runIndex]
  );

  const getBuffer = useCallback(
    (workflowId: string | null | undefined, stageNumber: number, runId: string): WSMessage[] | null => {
      return buffers.get(bufferKey(workflowId ?? null, stageNumber, runId)) ?? null;
    },
    [buffers]
  );

  return {
    status,
    pendingQuestions,
    sendAction,
    sendAgentResponse,
    getLiveRuns,
    getBuffer,
    lastEvent,
  };
}
