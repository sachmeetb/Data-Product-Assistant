import React, { createContext, useContext, useMemo } from "react";
import type { ToastTone } from "../lib/toastBus";

export interface ConfirmOptions {
  title: string;
  message?: React.ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  tone?: "default" | "danger";
}

export interface PromptOptions {
  title: string;
  message?: React.ReactNode;
  label?: string;
  placeholder?: string;
  defaultValue?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  multiline?: boolean;
}

export type NotifyTone = ToastTone; // "error" | "warning" | "success" | "info"

export interface NotifyOptions {
  title: string;
  message?: React.ReactNode;
  tone?: NotifyTone;
  /** Pre-formatted technical detail shown behind a "Show details" toggle. */
  details?: string;
  confirmLabel?: string;
}

export interface ToastInput {
  message: string;
  tone?: NotifyTone;
  dedupeKey?: string;
  durationMs?: number;
}

export interface DialogContextValue {
  confirm: (opts: ConfirmOptions) => Promise<boolean>;
  prompt: (opts: PromptOptions) => Promise<string | null>;
  /** Explicit notice (any tone), rendered as a modal. */
  notify: (opts: NotifyOptions) => Promise<void>;
  /** Normalize any thrown error and show it as an error-tone modal. */
  showError: (e: unknown, opts?: { title?: string; fallback?: string }) => Promise<void>;
  /** Transient, non-blocking toast. */
  toast: (input: string | ToastInput) => void;
}

export const DialogContext = createContext<DialogContextValue | null>(null);

function useDialogContext(): DialogContextValue {
  const ctx = useContext(DialogContext);
  if (!ctx) {
    throw new Error("dialog hooks (useConfirm/usePrompt/useNotify/useToast) must be used within a ConfirmProvider");
  }
  return ctx;
}

/** Async confirm() → Promise<boolean>. */
export function useConfirm() {
  return useDialogContext().confirm;
}

/** Async prompt() → Promise<string | null> (null on cancel). */
export function usePrompt() {
  return useDialogContext().prompt;
}

/** { notify, showError } — structured error / notice dialogs. */
export function useNotify() {
  const { notify, showError } = useDialogContext();
  return useMemo(() => ({ notify, showError }), [notify, showError]);
}

/** Transient toast raiser. */
export function useToast() {
  return useDialogContext().toast;
}
