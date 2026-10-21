import React from "react";

interface ErrorBoundaryState {
  error: Error | null;
  componentStack: string;
}

/** App-wide crash boundary. Mounted outermost in main.tsx so it wraps
 *  BrowserRouter / AuthProvider / ConfirmProvider. It catches **render and
 *  lifecycle** errors only — event-handler and async failures don't reach a
 *  boundary and are surfaced by showError() at their call sites. The fallback
 *  renders its own inline card (it can't use the dialog hook once the tree has
 *  crashed) with one-click copy + reload. */
export default class ErrorBoundary extends React.Component<
  { children: React.ReactNode },
  ErrorBoundaryState
> {
  state: ErrorBoundaryState = { error: null, componentStack: "" };

  static getDerivedStateFromError(error: Error): Partial<ErrorBoundaryState> {
    return { error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    this.setState({ componentStack: info.componentStack ?? "" });
    console.error("Unhandled render error:", error, info);
  }

  private copy = () => {
    const { error, componentStack } = this.state;
    const bundle = [
      error?.message ?? "Unknown error",
      "",
      error?.stack ?? "",
      componentStack ? `\ncomponent stack:${componentStack}` : "",
      "",
      new Date().toISOString(),
    ].join("\n");
    navigator.clipboard?.writeText(bundle).catch(() => {});
  };

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;

    const btn: React.CSSProperties = {
      padding: "8px 16px", borderRadius: 6, fontSize: 13, fontWeight: 700, cursor: "pointer",
    };
    return (
      <div
        style={{
          position: "fixed", inset: 0, backgroundColor: "rgba(15, 23, 42, 0.55)",
          display: "flex", alignItems: "center", justifyContent: "center", zIndex: 3000,
          fontFamily: "system-ui, sans-serif",
        }}
      >
        <div
          role="alertdialog"
          aria-label="Application error"
          style={{
            width: 480, maxWidth: "calc(100vw - 32px)", maxHeight: "calc(100vh - 40px)",
            overflowY: "auto", backgroundColor: "#fff", borderRadius: 12, padding: 24,
            boxShadow: "0 20px 40px rgba(15, 23, 42, 0.25)",
            display: "flex", flexDirection: "column", gap: 14,
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <div
              aria-hidden
              style={{
                width: 28, height: 28, borderRadius: "50%", display: "flex",
                alignItems: "center", justifyContent: "center", backgroundColor: "#fef2f2",
                color: "#dc2626", border: "1px solid #fecaca", fontSize: 15, fontWeight: 700,
              }}
            >
              ✕
            </div>
            <div style={{ fontSize: 17, fontWeight: 700, color: "#0f172a" }}>
              Something went wrong
            </div>
          </div>
          <div style={{ fontSize: 13, color: "#334155", lineHeight: 1.5 }}>
            The page hit an unexpected error and couldn't continue. You can copy the
            details for a bug report, then reload.
          </div>
          <pre
            style={{
              margin: 0, padding: 12, backgroundColor: "#f8fafc", border: "1px solid #e2e8f0",
              borderRadius: 6, color: "#334155", fontSize: 12, lineHeight: 1.45,
              whiteSpace: "pre-wrap", wordBreak: "break-word", maxHeight: 200, overflowY: "auto",
            }}
          >
            {error.message}
          </pre>
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 10 }}>
            <button
              type="button" onClick={this.copy}
              style={{ ...btn, backgroundColor: "#fff", color: "#334155", border: "1px solid #cbd5e1", fontWeight: 600 }}
            >
              Copy details
            </button>
            <button
              type="button" onClick={() => window.location.reload()}
              style={{ ...btn, backgroundColor: "#dc2626", color: "#fff", border: "none" }}
            >
              Reload
            </button>
          </div>
        </div>
      </div>
    );
  }
}
