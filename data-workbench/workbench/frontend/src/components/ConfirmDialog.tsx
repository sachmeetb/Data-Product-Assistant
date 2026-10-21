import React, { useEffect, useId, useRef } from "react";
import ModalShell from "./ModalShell";

export interface ConfirmDialogProps {
  open: boolean;
  title: string;
  message: React.ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  tone?: "default" | "danger";
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

/** In-app replacement for window.confirm — renders through the shared ModalShell
 *  (backdrop, focus trap, Escape, aria). `tone="danger"` renders a red primary
 *  for destructive actions and autofocuses Cancel instead of the primary. */
export default function ConfirmDialog({
  open,
  title,
  message,
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  tone = "default",
  busy = false,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const titleId = useId();
  const primaryRef = useRef<HTMLButtonElement | null>(null);
  const cancelRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const t = setTimeout(() => {
      (tone === "danger" ? cancelRef : primaryRef).current?.focus();
    }, 0);
    return () => clearTimeout(t);
  }, [open, tone]);

  const primaryBg = busy ? "#cbd5e1" : tone === "danger" ? "#dc2626" : "#3b82f6";

  return (
    <ModalShell open={open} labelledById={titleId} onClose={onCancel} closeDisabled={busy} width={460}>
      <div id={titleId} style={{ fontSize: 17, fontWeight: 700, color: "#0f172a" }}>{title}</div>
      <div style={{ fontSize: 13, color: "#334155", lineHeight: 1.5 }}>{message}</div>
      <div style={{ display: "flex", justifyContent: "flex-end", gap: 10 }}>
        <button
          ref={cancelRef}
          type="button" onClick={onCancel} disabled={busy}
          style={{
            padding: "8px 16px", borderRadius: 6, backgroundColor: "#fff",
            color: "#334155", border: "1px solid #cbd5e1", fontSize: 13,
            fontWeight: 600, cursor: busy ? "not-allowed" : "pointer",
          }}
        >
          {cancelLabel}
        </button>
        <button
          ref={primaryRef}
          type="button" onClick={onConfirm} disabled={busy}
          style={{
            padding: "8px 16px", borderRadius: 6, backgroundColor: primaryBg,
            color: "#fff", border: "none", fontSize: 13, fontWeight: 700,
            cursor: busy ? "not-allowed" : "pointer",
          }}
        >
          {busy ? "…" : confirmLabel}
        </button>
      </div>
    </ModalShell>
  );
}
