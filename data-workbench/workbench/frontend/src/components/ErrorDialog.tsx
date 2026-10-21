import React, { useEffect, useId, useRef, useState } from "react";
import ModalShell from "./ModalShell";
import type { NotifyTone } from "./dialogContext";

export interface ErrorDialogProps {
  open: boolean;
  tone: NotifyTone;
  title: string;
  message?: React.ReactNode;
  /** Pre-formatted technical block; when absent the details toggle is hidden. */
  details?: string;
  /** Plaintext bundle copied by the Copy button. */
  copyText: string;
  confirmLabel?: string;
  onClose: () => void;
}

interface ToneSpec { fg: string; bg: string; border: string; glyph: string; }

const TONES: Record<NotifyTone, ToneSpec> = {
  error: { fg: "#dc2626", bg: "#fef2f2", border: "#fecaca", glyph: "✕" },
  warning: { fg: "#b45309", bg: "#fffbeb", border: "#fde68a", glyph: "⚠" },
  success: { fg: "#15803d", bg: "#f0fdf4", border: "#bbf7d0", glyph: "✓" },
  info: { fg: "#2563eb", bg: "#eff6ff", border: "#bfdbfe", glyph: "ℹ" },
};

/** Standardized in-app replacement for window.alert — a structured error/notice
 *  modal with severity tones, one-click copy, and an expandable technical
 *  details pane. Rendered above other modals (z-index 2000) so an error raised
 *  from inside another dialog still shows on top. */
export default function ErrorDialog({
  open,
  tone,
  title,
  message,
  details,
  copyText,
  confirmLabel = "OK",
  onClose,
}: ErrorDialogProps) {
  const titleId = useId();
  const okRef = useRef<HTMLButtonElement | null>(null);
  const [showDetails, setShowDetails] = useState(false);
  const [copied, setCopied] = useState(false);
  const spec = TONES[tone];

  // The provider remounts this per error (via a changing key), so showDetails /
  // copied start fresh — no reset effect needed. This effect only autofocuses OK.
  useEffect(() => {
    if (!open) return;
    const t = setTimeout(() => okRef.current?.focus(), 0);
    return () => clearTimeout(t);
  }, [open]);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(copyText);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard unavailable (insecure context) — ignore */
    }
  };

  const iconBtn: React.CSSProperties = {
    padding: "4px 8px", borderRadius: 6, border: "1px solid #cbd5e1",
    backgroundColor: "#fff", color: "#475569", fontSize: 12, fontWeight: 600,
    cursor: "pointer", lineHeight: 1.4,
  };

  return (
    <ModalShell open={open} labelledById={titleId} onClose={onClose} width={480} zIndex={2000}>
      <div style={{ display: "flex", alignItems: "flex-start", gap: 12 }}>
        <div
          aria-hidden
          style={{
            flex: "0 0 auto", width: 28, height: 28, borderRadius: "50%",
            display: "flex", alignItems: "center", justifyContent: "center",
            backgroundColor: spec.bg, color: spec.fg, border: `1px solid ${spec.border}`,
            fontSize: 15, fontWeight: 700,
          }}
        >
          {spec.glyph}
        </div>
        <div id={titleId} style={{ flex: 1, fontSize: 17, fontWeight: 700, color: "#0f172a", paddingTop: 3 }}>
          {title}
        </div>
        <div style={{ display: "flex", gap: 6, flex: "0 0 auto" }}>
          <button type="button" onClick={copy} style={iconBtn}>
            {copied ? "Copied ✓" : "Copy"}
          </button>
          <button
            type="button" onClick={onClose} aria-label="Close"
            style={{ ...iconBtn, padding: "4px 9px", fontSize: 16 }}
          >
            ×
          </button>
        </div>
      </div>

      {message != null && message !== "" && (
        <div style={{ fontSize: 13, color: "#334155", lineHeight: 1.5, whiteSpace: "pre-wrap" }}>
          {message}
        </div>
      )}

      {details && (
        <div>
          <button
            type="button"
            onClick={() => setShowDetails((v) => !v)}
            style={{
              background: "none", border: "none", padding: 0, cursor: "pointer",
              color: "#64748b", fontSize: 12, fontWeight: 600,
            }}
            aria-expanded={showDetails}
          >
            {showDetails ? "▾" : "▸"} {showDetails ? "Hide" : "Show"} technical details
          </button>
          {showDetails && (
            <pre
              style={{
                marginTop: 8, padding: 12, backgroundColor: "#f8fafc",
                border: "1px solid #e2e8f0", borderRadius: 6, color: "#334155",
                fontSize: 12, lineHeight: 1.45, whiteSpace: "pre-wrap", wordBreak: "break-word",
                maxHeight: 240, overflowY: "auto", margin: "8px 0 0",
              }}
            >
              {details}
            </pre>
          )}
        </div>
      )}

      <div style={{ display: "flex", justifyContent: "flex-end", gap: 10 }}>
        <button
          ref={okRef}
          type="button"
          onClick={onClose}
          style={{
            padding: "8px 16px", borderRadius: 6, backgroundColor: spec.fg,
            color: "#fff", border: "none", fontSize: 13, fontWeight: 700, cursor: "pointer",
          }}
        >
          {confirmLabel}
        </button>
      </div>
    </ModalShell>
  );
}
