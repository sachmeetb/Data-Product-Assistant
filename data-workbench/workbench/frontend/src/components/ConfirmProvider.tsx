import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ConfirmDialog from "./ConfirmDialog";
import PromptDialog from "./PromptDialog";
import ErrorDialog from "./ErrorDialog";
import ToastViewport, { type ActiveToast } from "./Toast";
import { DialogContext } from "./dialogContext";
import type {
  ConfirmOptions,
  PromptOptions,
  NotifyOptions,
  NotifyTone,
  ToastInput,
  DialogContextValue,
} from "./dialogContext";
import { subscribeToast } from "../lib/toastBus";
import { normalizeError, formatErrorForCopy } from "../lib/apiError";

interface ErrorView {
  tone: NotifyTone;
  title: string;
  message?: React.ReactNode;
  details?: string;
  copyText: string;
  confirmLabel?: string;
}

// One FIFO queue for all modal dialogs — only the head is shown, and requests
// resolve in order. This avoids the earlier one-resolver-per-type design, where
// a second request (e.g. a burst of failing calls) would overwrite the first
// and leave its promise unresolved forever.
type DialogRequest =
  | { id: number; kind: "confirm"; opts: ConfirmOptions; resolve: (v: boolean) => void }
  | { id: number; kind: "prompt"; opts: PromptOptions; resolve: (v: string | null) => void }
  | { id: number; kind: "error"; view: ErrorView; resolve: () => void };

const TOAST_CAP = 4;
const DEFAULT_TOAST_MS = 3500;

/** App-root provider exposing promise-based confirm()/prompt()/notify()/
 *  showError() dialogs plus toast(). Mounted once in App.tsx. Hooks live in
 *  dialogContext.ts (a non-component module) so this file only exports a
 *  component. */
export function ConfirmProvider({ children }: { children: React.ReactNode }) {
  const [queue, setQueue] = useState<DialogRequest[]>([]);
  const idRef = useRef(0);
  const current = queue[0] ?? null;

  const dequeue = useCallback(() => setQueue((q) => q.slice(1)), []);

  const confirm = useCallback(
    (opts: ConfirmOptions) =>
      new Promise<boolean>((resolve) => {
        const id = ++idRef.current;
        setQueue((q) => [...q, { id, kind: "confirm", opts, resolve }]);
      }),
    [],
  );

  const prompt = useCallback(
    (opts: PromptOptions) =>
      new Promise<string | null>((resolve) => {
        const id = ++idRef.current;
        setQueue((q) => [...q, { id, kind: "prompt", opts, resolve }]);
      }),
    [],
  );

  const showErrorView = useCallback(
    (view: ErrorView) =>
      new Promise<void>((resolve) => {
        const id = ++idRef.current;
        setQueue((q) => [...q, { id, kind: "error", view, resolve }]);
      }),
    [],
  );

  const notify = useCallback(
    (opts: NotifyOptions) => {
      const msgStr = typeof opts.message === "string" ? opts.message : "";
      const copyText = [
        opts.title,
        "",
        msgStr,
        ...(opts.details ? ["", "── technical details ──", opts.details] : []),
      ]
        .join("\n")
        .trim();
      return showErrorView({
        tone: opts.tone ?? "info",
        title: opts.title,
        message: opts.message,
        details: opts.details,
        copyText,
        confirmLabel: opts.confirmLabel,
      });
    },
    [showErrorView],
  );

  const showError = useCallback(
    (e: unknown, opts?: { title?: string; fallback?: string }) => {
      const title = opts?.title ?? "Something went wrong";
      const n = normalizeError(e, { fallback: opts?.fallback });
      return showErrorView({
        tone: "error",
        title,
        message: n.message,
        details: n.details,
        copyText: formatErrorForCopy(title, n),
      });
    },
    [showErrorView],
  );

  // ---- toasts ----
  const [toasts, setToasts] = useState<ActiveToast[]>([]);
  const toastIdRef = useRef(0);

  const removeToast = useCallback((id: number) => {
    setToasts((cur) => cur.filter((t) => t.id !== id));
  }, []);

  const pushToast = useCallback((input: string | ToastInput) => {
    const payload: ToastInput = typeof input === "string" ? { message: input } : input;
    const tone: NotifyTone = payload.tone ?? "info";
    const durationMs = payload.durationMs ?? DEFAULT_TOAST_MS;
    const candidateId = ++toastIdRef.current;
    setToasts((cur) => {
      if (payload.dedupeKey) {
        const existing = cur.find((t) => t.dedupeKey === payload.dedupeKey);
        if (existing) {
          // Refresh the visible toast (message + reset timer via nonce) instead
          // of stacking a duplicate.
          return cur.map((t) =>
            t.id === existing.id
              ? { ...t, message: payload.message, tone, durationMs, nonce: t.nonce + 1 }
              : t,
          );
        }
      }
      const item: ActiveToast = {
        id: candidateId,
        message: payload.message,
        tone,
        durationMs,
        dedupeKey: payload.dedupeKey,
        nonce: 0,
      };
      const next = [...cur, item];
      return next.length > TOAST_CAP ? next.slice(next.length - TOAST_CAP) : next;
    });
  }, []);

  // Bridge the toast bus (non-React callers, e.g. the axios interceptor) into
  // provider-owned toast state. subscribeToast flushes any pre-mount buffer.
  useEffect(() => subscribeToast((t) => pushToast(t)), [pushToast]);

  const value = useMemo<DialogContextValue>(
    () => ({ confirm, prompt, notify, showError, toast: pushToast }),
    [confirm, prompt, notify, showError, pushToast],
  );

  return (
    <DialogContext.Provider value={value}>
      {children}
      {current?.kind === "confirm" && (
        <ConfirmDialog
          open
          title={current.opts.title}
          message={current.opts.message ?? ""}
          confirmLabel={current.opts.confirmLabel}
          cancelLabel={current.opts.cancelLabel}
          tone={current.opts.tone}
          onConfirm={() => { current.resolve(true); dequeue(); }}
          onCancel={() => { current.resolve(false); dequeue(); }}
        />
      )}
      {current?.kind === "prompt" && (
        <PromptDialog
          key={current.id}
          open
          title={current.opts.title}
          message={current.opts.message}
          label={current.opts.label}
          placeholder={current.opts.placeholder}
          defaultValue={current.opts.defaultValue}
          confirmLabel={current.opts.confirmLabel}
          cancelLabel={current.opts.cancelLabel}
          multiline={current.opts.multiline}
          onConfirm={(v) => { current.resolve(v); dequeue(); }}
          onCancel={() => { current.resolve(null); dequeue(); }}
        />
      )}
      {current?.kind === "error" && (
        <ErrorDialog
          key={current.id}
          open
          tone={current.view.tone}
          title={current.view.title}
          message={current.view.message}
          details={current.view.details}
          copyText={current.view.copyText}
          confirmLabel={current.view.confirmLabel}
          onClose={() => { current.resolve(); dequeue(); }}
        />
      )}
      <ToastViewport toasts={toasts} onDismiss={removeToast} />
    </DialogContext.Provider>
  );
}
