import React, { useEffect, useId, useRef, useState } from "react";
import ModalShell from "./ModalShell";

export interface PromptDialogProps {
  open: boolean;
  title: string;
  message?: React.ReactNode;
  label?: string;
  placeholder?: string;
  defaultValue?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  multiline?: boolean;
  busy?: boolean;
  /** Called with the entered string on submit. */
  onConfirm: (value: string) => void;
  /** Called on cancel / overlay dismiss (resolves the prompt to null). */
  onCancel: () => void;
}

/** In-app replacement for window.prompt — controlled input/textarea, rendered
 *  through the shared ModalShell. */
export default function PromptDialog({
  open,
  title,
  message,
  label,
  placeholder,
  defaultValue = "",
  confirmLabel = "OK",
  cancelLabel = "Cancel",
  multiline = false,
  busy = false,
  onConfirm,
  onCancel,
}: PromptDialogProps) {
  // The provider remounts this component (via a changing key) on each open, so
  // useState(defaultValue) is the fresh initial value — no reset effect needed.
  const [value, setValue] = useState(defaultValue);
  const titleId = useId();
  const inputRef = useRef<HTMLInputElement | HTMLTextAreaElement | null>(null);

  useEffect(() => {
    if (open) {
      const t = setTimeout(() => inputRef.current?.focus(), 0);
      return () => clearTimeout(t);
    }
  }, [open]);

  const submit = () => {
    if (!busy) onConfirm(value);
  };

  const fieldStyle: React.CSSProperties = {
    width: "100%", padding: "9px 12px", borderRadius: 6, border: "1px solid #cbd5e1",
    fontSize: 13, boxSizing: "border-box", fontFamily: "inherit",
  };

  return (
    <ModalShell open={open} labelledById={titleId} onClose={onCancel} closeDisabled={busy} width={480}>
      <div id={titleId} style={{ fontSize: 17, fontWeight: 700, color: "#0f172a" }}>{title}</div>
      {message && (
        <div style={{ fontSize: 13, color: "#334155", lineHeight: 1.5 }}>{message}</div>
      )}
      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        {label && (
          <label style={{ fontSize: 12, fontWeight: 600, color: "#475569" }}>{label}</label>
        )}
        {multiline ? (
          <textarea
            ref={(el) => { inputRef.current = el; }}
            value={value}
            placeholder={placeholder}
            disabled={busy}
            rows={4}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={(e) => {
              if ((e.metaKey || e.ctrlKey) && e.key === "Enter") submit();
            }}
            style={{ ...fieldStyle, resize: "vertical" }}
          />
        ) : (
          <input
            ref={(el) => { inputRef.current = el; }}
            type="text"
            value={value}
            placeholder={placeholder}
            disabled={busy}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") submit();
            }}
            style={fieldStyle}
          />
        )}
      </div>
      <div style={{ display: "flex", justifyContent: "flex-end", gap: 10 }}>
        <button
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
          type="button" onClick={submit} disabled={busy}
          style={{
            padding: "8px 16px", borderRadius: 6,
            backgroundColor: busy ? "#cbd5e1" : "#3b82f6",
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
