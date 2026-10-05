import { useCallback, useEffect, useRef, useState } from "react";
import { wsUrl } from "../api/client";
import type { ChatWSMessage } from "../types";

export type ChatSocketStatus = "disconnected" | "connected" | "streaming";

export function useChatSocket(projectId: number | null) {
  const [status, setStatus] = useState<ChatSocketStatus>("disconnected");
  const [lastEvent, setLastEvent] = useState<ChatWSMessage | null>(null);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    if (projectId === null) return;
    setLastEvent(null);
    const ws = new WebSocket(wsUrl(`/ws/chat/${projectId}`));
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
  }, [projectId]);

  const sendMessage = useCallback(
    (
      text: string,
      sessionId: number | null,
      attachments?: Array<{ filename: string; mime: string; content_base64: string }>,
    ) => {
      if (wsRef.current?.readyState !== WebSocket.OPEN) return false;
      wsRef.current.send(JSON.stringify({
        action: "send_message",
        text,
        session_id: sessionId,
        attachments: attachments && attachments.length > 0 ? attachments : undefined,
      }));
      return true;
    },
    []
  );

  return { status, lastEvent, sendMessage };
}
