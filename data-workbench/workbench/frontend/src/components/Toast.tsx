import { useEffect } from "react";
import type { NotifyTone } from "./dialogContext";

export interface ActiveToast {
  id: number;
  message: string;
  tone: NotifyTone;
  durationMs: number;
  dedupeKey?: string;
  /** Bumped when a dedupe'd toast is refreshed, to reset its dismiss timer. */
  nonce: number;
}

const TOAST_TONES: Record<NotifyTone, { bg: string; glyph: string }> = {
  error: { bg: "#b91c1c", glyph: "✕" },
  warning: { bg: "#b45309", glyph: "⚠" },
  success: { bg: "#15803d", glyph: "✓" },
  info: { bg: "#334155", glyph: "ℹ" },
};

/** Presentational toast stack. State (dedup, cap, timing) is owned by the
 *  provider; each item runs its own auto-dismiss timer, reset when `nonce`
 *  changes so a dedupe refresh restarts the countdown. */
export default function ToastViewport({
  toasts,
  onDismiss,
}: {
  toasts: ActiveToast[];
  onDismiss: (id: number) => void;
}) {
  if (!toasts.length) return null;
  return (
    <div
      style={{
        position: "fixed", bottom: 20, left: "50%", transform: "translateX(-50%)",
        zIndex: 9999, display: "flex", flexDirection: "column", gap: 8,
        alignItems: "center", pointerEvents: "none",
      }}
    >
      {toasts.map((t) => (
        <ToastItem key={t.id} toast={t} onDismiss={onDismiss} />
      ))}
    </div>
  );
}

function ToastItem({ toast, onDismiss }: { toast: ActiveToast; onDismiss: (id: number) => void }) {
  const spec = TOAST_TONES[toast.tone];

  useEffect(() => {
    const timer = setTimeout(() => onDismiss(toast.id), toast.durationMs);
    return () => clearTimeout(timer);
  }, [toast.id, toast.nonce, toast.durationMs, onDismiss]);

  return (
    <div
      role="status"
      style={{
        pointerEvents: "auto", background: spec.bg, color: "#fff", padding: "10px 16px",
        borderRadius: 8, fontSize: 13, boxShadow: "0 4px 12px rgba(0,0,0,0.25)",
        display: "flex", alignItems: "center", gap: 10,
        maxWidth: "min(560px, calc(100vw - 32px))",
      }}
    >
      <span aria-hidden style={{ fontWeight: 700 }}>{spec.glyph}</span>
      <span style={{ flex: 1 }}>{toast.message}</span>
      <button
        type="button" onClick={() => onDismiss(toast.id)} aria-label="Dismiss"
        style={{
          background: "none", border: "none", color: "#fff", opacity: 0.8,
          cursor: "pointer", fontSize: 16, lineHeight: 1, padding: 0,
        }}
      >
        ×
      </button>
    </div>
  );
}
