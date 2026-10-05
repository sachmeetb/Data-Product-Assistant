import React, { useEffect, useRef } from "react";

export interface ModalShellProps {
  open: boolean;
  /** id of the element that titles the dialog, wired to aria-labelledby. */
  labelledById?: string;
  /** Called on Escape and backdrop click (unless closeDisabled). */
  onClose: () => void;
  /** When true, Escape + backdrop clicks are ignored (e.g. while busy). */
  closeDisabled?: boolean;
  zIndex?: number;
  width?: number;
  children: React.ReactNode;
}

const FOCUSABLE =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), ' +
  'select:not([disabled]), [tabindex]:not([tabindex="-1"])';

/** Shared modal shell — backdrop + centered card with accessibility baked in:
 *  role="dialog", aria-modal, focus trap, focus restore, and Escape-to-close.
 *  Confirm/Prompt/Error dialogs all render through this so a11y lives in one
 *  place. Deliberately has NO global Enter handler — each dialog autofocuses its
 *  default button so Enter activates it natively (without hijacking Enter when
 *  Copy or a details toggle is focused). */
export default function ModalShell({
  open,
  labelledById,
  onClose,
  closeDisabled = false,
  zIndex = 1000,
  width = 460,
  children,
}: ModalShellProps) {
  const cardRef = useRef<HTMLDivElement | null>(null);
  const restoreRef = useRef<HTMLElement | null>(null);

  // Remember what had focus before opening; restore it on close/unmount. Also
  // pull focus into the card if a child didn't already grab it, so the card's
  // keydown handler (Escape / focus-trap) actually receives keys.
  useEffect(() => {
    if (!open) return;
    restoreRef.current = document.activeElement as HTMLElement | null;
    const t = setTimeout(() => {
      const card = cardRef.current;
      if (card && !card.contains(document.activeElement)) card.focus();
    }, 0);
    return () => {
      clearTimeout(t);
      restoreRef.current?.focus?.();
    };
  }, [open]);

  if (!open) return null;

  const handleKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Escape") {
      if (closeDisabled) return;
      e.stopPropagation();
      onClose();
      return;
    }
    if (e.key !== "Tab") return;
    const card = cardRef.current;
    if (!card) return;
    const nodes = Array.from(card.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
      (el) => el.offsetParent !== null,
    );
    if (nodes.length === 0) {
      e.preventDefault();
      card.focus();
      return;
    }
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    const active = document.activeElement as HTMLElement | null;
    if (e.shiftKey) {
      if (active === first || !card.contains(active)) {
        e.preventDefault();
        last.focus();
      }
    } else if (active === last || !card.contains(active)) {
      e.preventDefault();
      first.focus();
    }
  };

  return (
    <div
      onClick={(e) => {
        if (!closeDisabled && e.target === e.currentTarget) onClose();
      }}
      style={{
        position: "fixed", inset: 0, backgroundColor: "rgba(15, 23, 42, 0.55)",
        display: "flex", alignItems: "center", justifyContent: "center", zIndex,
      }}
    >
      <div
        ref={cardRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={labelledById}
        tabIndex={-1}
        onKeyDown={handleKeyDown}
        style={{
          width, maxWidth: "calc(100vw - 32px)", maxHeight: "calc(100vh - 40px)",
          overflowY: "auto", backgroundColor: "#fff", borderRadius: 12, padding: 24,
          boxShadow: "0 20px 40px rgba(15, 23, 42, 0.25)", outline: "none",
          display: "flex", flexDirection: "column", gap: 16,
        }}
      >
        {children}
      </div>
    </div>
  );
}
