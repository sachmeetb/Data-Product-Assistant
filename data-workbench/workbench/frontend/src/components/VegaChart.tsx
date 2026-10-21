import { useEffect, useRef, useState } from "react";

/**
 * Renders a Vega-Lite spec emitted by the marketplace chat's synthesis pass.
 * `vega` + `vega-lite` + `vega-embed` are heavy, so we lazy-load `vega-embed`
 * on first render — same dynamic-import pattern the MarkdownMessage component
 * uses for mermaid. On any failure we render nothing (the result table is
 * always shown by the caller regardless).
 */
export default function VegaChart({ spec }: { spec: Record<string, unknown> }) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    let view: { finalize: () => void } | null = null;
    (async () => {
      if (!containerRef.current || !spec) return;
      try {
        const vegaEmbed = (await import("vega-embed")).default;
        // Force a responsive width + clean defaults unless the spec says otherwise.
        const merged = { width: "container", ...spec } as Record<string, unknown>;
        const result = await vegaEmbed(containerRef.current, merged as never, {
          actions: false,
          renderer: "svg",
        });
        if (cancelled) {
          result.view.finalize();
          return;
        }
        view = result.view;
      } catch {
        if (!cancelled) setFailed(true);
      }
    })();
    return () => {
      cancelled = true;
      if (view) view.finalize();
    };
  }, [spec]);

  if (failed) return null;

  return (
    <div
      ref={containerRef}
      style={{
        width: "100%",
        padding: 8,
        background: "#fff",
        border: "1px solid #e2e8f0",
        borderRadius: 6,
        overflow: "auto",
      }}
    />
  );
}
