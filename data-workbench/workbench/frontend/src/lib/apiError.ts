/**
 * Extract a human-readable message from an error thrown by an axios call.
 *
 * FastAPI returns actionable errors in the response body as `{ detail: ... }`.
 * Axios's own `error.message` is the generic "Request failed with status code
 * NNN", which hides that detail — so prefer the backend `detail` (string, or
 * the FastAPI 422 validation array) and fall back to the axios message.
 */
export function apiErrorMessage(e: unknown): string {
  const err = e as {
    response?: { data?: { detail?: unknown } };
    message?: string;
  };
  const detail = err?.response?.data?.detail;
  if (typeof detail === "string" && detail.trim()) return detail;
  if (Array.isArray(detail)) {
    // FastAPI request-validation error shape: [{ loc, msg, ... }, ...]
    const msgs = detail
      .map((d) => (d && typeof d === "object" && "msg" in d ? String((d as { msg: unknown }).msg) : null))
      .filter(Boolean);
    if (msgs.length) return msgs.join("; ");
  }
  if (detail != null && typeof detail !== "object") return String(detail);
  return err?.message || String(e);
}

/** Rich, structured form of an error for the standardized ErrorDialog: a human
 *  summary plus an optional pre-formatted technical block (HTTP line, error
 *  class, raw body / stack). Covers the common backend shapes with a graceful
 *  pretty-print fallback for anything unrecognized. */
export interface NormalizedError {
  message: string;
  /** Pre-formatted technical detail; undefined ⇒ the details pane is hidden. */
  details?: string;
  status?: number;
  method?: string;
  url?: string;
  errorClass?: string;
}

const SENSITIVE_QS = [
  "token", "api_key", "apikey", "access_token", "password", "authorization", "secret",
];
const MAX_DETAILS = 8000;

/** Replace known-sensitive query-string values with *** so a copied error
 *  bundle never leaks credentials. Preserves relative-vs-absolute form. */
function redactUrl(url?: string): string | undefined {
  if (!url) return url;
  try {
    const u = new URL(url, "http://_");
    let changed = false;
    u.searchParams.forEach((_v, k) => {
      if (SENSITIVE_QS.includes(k.toLowerCase())) {
        u.searchParams.set(k, "***");
        changed = true;
      }
    });
    if (!changed) return url;
    const isRelative = !/^https?:\/\//i.test(url);
    return isRelative ? `${u.pathname}${u.search}` : u.toString();
  } catch {
    return url;
  }
}

/** JSON.stringify that survives circular refs and BigInt. */
function safeStringify(v: unknown): string {
  const seen = new WeakSet<object>();
  try {
    return (
      JSON.stringify(
        v,
        (_k, val) => {
          if (typeof val === "bigint") return String(val);
          if (typeof val === "object" && val !== null) {
            if (seen.has(val)) return "[Circular]";
            seen.add(val);
          }
          return val;
        },
        2,
      ) ?? String(v)
    );
  } catch {
    return String(v);
  }
}

function truncate(s: string): string {
  return s.length > MAX_DETAILS ? `${s.slice(0, MAX_DETAILS)}\n…(truncated)` : s;
}

export function normalizeError(e: unknown, opts?: { fallback?: string }): NormalizedError {
  const fallback = opts?.fallback ?? "Something went wrong.";
  const err = e as {
    response?: { status?: number; data?: unknown };
    config?: { method?: string; url?: string };
    message?: string;
    stack?: string;
  };

  const status = err?.response?.status;
  const method = err?.config?.method ? err.config.method.toUpperCase() : undefined;
  const url = redactUrl(err?.config?.url);
  const data = err?.response?.data as
    | { detail?: unknown; error_class?: unknown; error_message?: unknown; message?: unknown; error?: unknown }
    | undefined;
  const detail = data?.detail;

  // ---- human summary ----
  let message = "";
  if (typeof detail === "string" && detail.trim()) {
    message = detail;
  } else if (Array.isArray(detail)) {
    // FastAPI 422 validation: [{ loc, msg, ... }, ...]
    message = detail
      .map((d) => (d && typeof d === "object" && "msg" in d ? String((d as { msg: unknown }).msg) : null))
      .filter(Boolean)
      .join("; ");
  } else if (detail && typeof detail === "object") {
    const d = detail as { message?: unknown; missing?: unknown; errors?: unknown; reason?: unknown };
    if (typeof d.message === "string" && d.message.trim()) {
      message = d.message;
      if (Array.isArray(d.missing) && d.missing.length) {
        message += `\n\n• ${d.missing.map(String).join("\n• ")}`;
      }
    } else if (Array.isArray(d.errors) && d.errors.length) {
      message = d.errors.map(String).join("\n");
    } else if (typeof d.reason === "string" && d.reason.trim()) {
      message = d.reason;
    } else {
      message = safeStringify(detail);
    }
  } else if (detail != null && typeof detail !== "object") {
    message = String(detail);
  }

  if (!message) {
    // 200-body soft errors and other shapes.
    if (typeof data?.message === "string" && data.message.trim()) message = data.message;
    else if (typeof data?.error === "string" && data.error.trim()) message = data.error;
    else if (typeof data?.error_message === "string" && data.error_message.trim()) message = data.error_message;
    else if (typeof err?.message === "string" && err.message.trim()) message = err.message;
    else message = fallback;
  }

  // ---- error_class (root OR inside detail) ----
  let errorClass: string | undefined;
  const rootEc = data?.error_class;
  const detailEc = detail && typeof detail === "object" ? (detail as { error_class?: unknown }).error_class : undefined;
  if (typeof rootEc === "string") errorClass = rootEc;
  else if (typeof detailEc === "string") errorClass = detailEc;

  // ---- technical details block ----
  const lines: string[] = [];
  const httpLine = `${method ?? ""} ${url ?? ""}`.trim();
  if (httpLine || status != null) {
    lines.push(httpLine + (status != null ? ` → ${status}` : ""));
  }
  if (errorClass) lines.push(`error_class: ${errorClass}`);
  if (data !== undefined) {
    lines.push("", typeof data === "string" ? data : safeStringify(data));
  } else if (err?.stack) {
    lines.push("", err.stack);
  }
  const detailsRaw = lines.join("\n").trim();
  const details = detailsRaw ? truncate(detailsRaw) : undefined;

  return { message, details, status, method, url, errorClass };
}

/** Render a normalized error as a plaintext bundle for one-click copy. */
export function formatErrorForCopy(title: string, n: NormalizedError): string {
  const parts = [title, "", n.message];
  if (n.details) parts.push("", "── technical details ──", n.details);
  parts.push("", new Date().toISOString());
  return parts.join("\n");
}
