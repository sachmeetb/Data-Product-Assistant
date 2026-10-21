import { useCallback, useEffect, useRef, useState } from "react";
import { wsUrl } from "../api/client";
import type { ChatWSMessage } from "../types";

export type ProductChatSocketStatus = "disconnected" | "connected" | "streaming";

/**
 * WebSocket hook for the Product Workbench chat drawer. Now keyed by
 * ``ownerEmail`` so persisted sessions can be scoped per user (the chat
 * may be opened before any project exists, e.g. during the early steps
 * of the New Product wizard). Each ``send_message`` carries an optional
 * ``session_id``; when null, the backend creates a new session and echoes
 * its id back via ``turn_started`` so the panel can land on it.
 */
export function useProductChatSocket(ownerEmail: string | null) {
  const [status, setStatus] = useState<ProductChatSocketStatus>("disconnected");
  const [lastEvent, setLastEvent] = useState<ChatWSMessage | null>(null);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    if (!ownerEmail) return;
    setLastEvent(null);
    const ws = new WebSocket(wsUrl(`/ws/product-chat/${encodeURIComponent(ownerEmail)}`));
    wsRef.current = ws;
    ws.onopen = () => setStatus("connected");
    ws.onclose = () => setStatus("disconnected");
    ws.onerror = () => setStatus("disconnected");
    ws.onmessage = (event) => {
      const msg: ChatWSMessage = JSON.parse(event.data);
      setLastEvent(msg);
      if (msg.type === "turn_started") setStatus("streaming");
      if (msg.type === "turn_complete" || msg.type === "error") setStatus("connected");
    };
    return () => {
      ws.close();
      wsRef.current = null;
    };
  }, [ownerEmail]);

  const sendMessage = useCallback(
    (
      text: string,
      projectId: number | null,
      context: Record<string, unknown> | null,
      sessionId: number | null,
      attachments?: Array<{ filename: string; mime: string; content_base64: string }>,
    ) => {
      if (wsRef.current?.readyState !== WebSocket.OPEN) return false;
      wsRef.current.send(JSON.stringify({
        action: "send_message",
        text,
        project_id: projectId,
        context: context ?? null,
        session_id: sessionId ?? null,
        attachments: attachments && attachments.length > 0 ? attachments : undefined,
      }));
      return true;
    },
    []
  );

  const deleteSession = useCallback((sessionId: number) => {
    if (wsRef.current?.readyState !== WebSocket.OPEN) return false;
    wsRef.current.send(JSON.stringify({ action: "delete_session", session_id: sessionId }));
    return true;
  }, []);

  return { status, lastEvent, sendMessage, deleteSession };
}
