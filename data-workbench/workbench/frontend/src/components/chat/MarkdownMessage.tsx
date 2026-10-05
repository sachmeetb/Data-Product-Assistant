import { useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

const MERMAID_DIRECTIVE_RE = /^\s*(graph|flowchart|sequenceDiagram|classDiagram|stateDiagram(?:-v2)?|erDiagram|journey|gantt|pie|mindmap|timeline|quadrantChart|requirementDiagram|gitGraph|C4Context)\b/;

function hashSource(s: string): string {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
  return `m${Math.abs(h).toString(36)}`;
}

function MermaidDiagram({ source }: { source: string }) {
  const trimmed = source.trim();
  const diagramId = useMemo(() => hashSource(trimmed), [trimmed]);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [svg, setSvg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const looksLikeMermaid = MERMAID_DIRECTIVE_RE.test(trimmed);

  useEffect(() => {
    if (!looksLikeMermaid) return;
    let cancelled = false;
    (async () => {
      try {
        const mermaid = (await import("mermaid")).default;
        mermaid.initialize({
          startOnLoad: false,
          theme: "default",
          securityLevel: "strict",
        });
        const { svg } = await mermaid.render(`${diagramId}-svg`, trimmed);
        if (!cancelled) {
          setSvg(svg);
          setError(null);
        }
      } catch (e) {
        if (!cancelled) {
          setSvg(null);
          setError(e instanceof Error ? e.message : String(e));
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [trimmed, diagramId, looksLikeMermaid]);

  if (!looksLikeMermaid) {
    return (
      <pre style={codeBlockStyle}>
        <code>{source}</code>
      </pre>
    );
  }

  if (error) {
    return (
      <div style={{ margin: "6px 0" }}>
        <div style={{ fontSize: 11, color: "#dc2626", marginBottom: 2 }}>
          Mermaid render error: {error}
        </div>
        <pre style={codeBlockStyle}>
          <code>{source}</code>
        </pre>
      </div>
    );
  }

  if (!svg) {
    return (
      <div style={{ fontSize: 11, color: "#94a3b8", margin: "6px 0" }}>
        Rendering diagram…
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      style={{
        margin: "6px 0",
        padding: 6,
        background: "white",
        border: "1px solid #e2e8f0",
        borderRadius: 4,
        overflow: "auto",
        maxWidth: "100%",
      }}
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  );
}

const codeBlockStyle: React.CSSProperties = {
  background: "#0f172a",
  color: "#a5f3fc",
  padding: 8,
  borderRadius: 4,
  fontSize: 11,
  overflowX: "auto",
  margin: "4px 0",
  fontFamily: "monospace",
  whiteSpace: "pre",
};

const inlineCodeStyle: React.CSSProperties = {
  background: "#e2e8f0",
  padding: "1px 4px",
  borderRadius: 3,
  fontSize: 12,
  fontFamily: "monospace",
};

const tableStyle: React.CSSProperties = {
  borderCollapse: "collapse",
  width: "100%",
  margin: "6px 0",
  fontSize: 12,
};

const cellStyle: React.CSSProperties = {
  padding: "4px 8px",
  border: "1px solid #e2e8f0",
  textAlign: "left",
  verticalAlign: "top",
};

const components: Components = {
  code({ className, children, ...rest }) {
    const isBlock = typeof className === "string" && className.startsWith("language-");
    const lang = isBlock ? className!.replace("language-", "") : "";
    const raw = String(children ?? "").replace(/\n$/, "");

    if (isBlock && lang === "mermaid") {
      return <MermaidDiagram source={raw} />;
    }
    if (isBlock) {
      return (
        <pre style={codeBlockStyle}>
          <code className={className} {...rest}>
            {children}
          </code>
        </pre>
      );
    }
    return (
      <code style={inlineCodeStyle} {...rest}>
        {children}
      </code>
    );
  },
  table({ children }) {
    return <table style={tableStyle}>{children}</table>;
  },
  thead({ children }) {
    return <thead style={{ background: "#f1f5f9" }}>{children}</thead>;
  },
  th({ children }) {
    return <th style={{ ...cellStyle, fontWeight: 600 }}>{children}</th>;
  },
  td({ children }) {
    return <td style={cellStyle}>{children}</td>;
  },
  h1: ({ children }) => (
    <h1 style={{ fontSize: 16, margin: "8px 0 4px", color: "#0f172a" }}>{children}</h1>
  ),
  h2: ({ children }) => (
    <h2 style={{ fontSize: 15, margin: "8px 0 4px", color: "#0f172a" }}>{children}</h2>
  ),
  h3: ({ children }) => (
    <h3 style={{ fontSize: 14, margin: "6px 0 3px", color: "#0f172a" }}>{children}</h3>
  ),
  h4: ({ children }) => (
    <h4 style={{ fontSize: 13, margin: "6px 0 3px", color: "#0f172a" }}>{children}</h4>
  ),
  p: ({ children }) => <p style={{ margin: "4px 0" }}>{children}</p>,
  ul: ({ children }) => (
    <ul style={{ margin: "4px 0", paddingLeft: 20 }}>{children}</ul>
  ),
  ol: ({ children }) => (
    <ol style={{ margin: "4px 0", paddingLeft: 20 }}>{children}</ol>
  ),
  li: ({ children }) => <li style={{ margin: "2px 0" }}>{children}</li>,
  blockquote: ({ children }) => (
    <blockquote
      style={{
        borderLeft: "3px solid #cbd5e1",
        paddingLeft: 8,
        margin: "6px 0",
        color: "#475569",
      }}
    >
      {children}
    </blockquote>
  ),
  a: ({ children, href }) => (
    <a href={href} target="_blank" rel="noopener noreferrer" style={{ color: "#2563eb" }}>
      {children}
    </a>
  ),
};

export default function MarkdownMessage({ content }: { content: string }) {
  return (
    <div style={{ lineHeight: 1.45 }}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {content}
      </ReactMarkdown>
    </div>
  );
}
