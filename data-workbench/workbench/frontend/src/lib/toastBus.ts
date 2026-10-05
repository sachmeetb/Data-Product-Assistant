// Module-level pub/sub so non-React code (the axios interceptor in api/client.ts)
// can raise toasts. The provider (ConfirmProvider) subscribes on mount and owns
// dedup/cap/timing. Events emitted before any subscriber exists are buffered and
// flushed on the first subscribe (client.ts can emit during app load).

export type ToastTone = "error" | "warning" | "success" | "info";

export interface ToastPayload {
  message: string;
  tone?: ToastTone;
  /** Toasts sharing a dedupeKey collapse to one (the timer refreshes). */
  dedupeKey?: string;
  durationMs?: number;
}

type Listener = (t: ToastPayload) => void;

const listeners = new Set<Listener>();
let buffer: ToastPayload[] = [];
const MAX_BUFFER = 10;

export function emitToast(t: ToastPayload): void {
  if (listeners.size === 0) {
    buffer.push(t);
    if (buffer.length > MAX_BUFFER) buffer = buffer.slice(buffer.length - MAX_BUFFER);
    return;
  }
  listeners.forEach((l) => l(t));
}

export function subscribeToast(listener: Listener): () => void {
  listeners.add(listener);
  if (buffer.length) {
    const pending = buffer;
    buffer = [];
    pending.forEach((t) => listener(t));
  }
  return () => {
    listeners.delete(listener);
  };
}
