import { productTheme } from "../../theme";

export type ProductVerdict = "reuse" | "extend";
export type TemplateVerdict = "clone";

export interface SimilarProduct {
  uri: string;
  dp_uri: string | null;
  contract_id: string | null;
  project_id: number | null;
  name: string;
  description: string;
  purpose: string;
  domain: string;
  lifecycle_state: string;
  owner_email: string;
  column_count: number;
  column_names: string[];
  match_score: number | null;
  delta: string;
  verdict: ProductVerdict;
  overlap_pct: number;
  missing_attributes: string[];
}

export interface MatchingTemplate {
  template_id: string;
  name: string;
  description: string;
  purpose: string;
  domain: string;
  column_count: number;
  column_names: string[];
  /** Parsed YAML used by the wizard's clone handler. Backend keeps this
   *  in `spec` so the frontend can populate Step 2 + Step 3 from it. */
  spec: Record<string, unknown>;
  match_score: number | null;
  delta: string;
  verdict: TemplateVerdict;
  overlap_pct: number;
  missing_attributes: string[];
}

export interface DiscoveryResult {
  similar_products: SimilarProduct[];
  matching_templates: MatchingTemplate[];
  recommended_columns: string[];
  rationale: string;
  dropped: number;
  error: string | null;
}

const LIFECYCLE_LABELS: Record<string, string> = {
  draft: "Draft",
  ingesting: "Ingesting",
  submitted: "Awaiting engineering",
  in_engineering: "In engineering",
  approved: "Ready to deploy",
  published: "Deployed",
  superseded: "Superseded",
  rejected: "Returned",
};

const LIFECYCLE_COLORS: Record<string, { bg: string; fg: string }> = {
  draft: { bg: "#f1f5f9", fg: "#475569" },
  ingesting: { bg: "#fef3c7", fg: "#92400e" },
  submitted: { bg: "#dbeafe", fg: "#1d4ed8" },
  in_engineering: { bg: "#fde68a", fg: "#92400e" },
  approved: { bg: "#ede9fe", fg: "#5b21b6" },
  published: { bg: "#dcfce7", fg: "#065f46" },
  superseded: { bg: "#fee2e2", fg: "#991b1b" },
  rejected: { bg: "#fee2e2", fg: "#991b1b" },
};

const VERDICT_LABELS: Record<string, string> = {
  reuse: "Reuse as-is",
  extend: "Extend / collaborate",
  clone: "Clone & edit",
};

const VERDICT_COLORS: Record<string, { bg: string; fg: string }> = {
  reuse: { bg: "#dcfce7", fg: "#065f46" },
  extend: { bg: "#fef3c7", fg: "#92400e" },
  clone: { bg: "#dbeafe", fg: "#1d4ed8" },
};

function VerdictBadge({ verdict }: { verdict: string }) {
  const palette = VERDICT_COLORS[verdict] || VERDICT_COLORS.extend;
  const label = VERDICT_LABELS[verdict] || verdict;
  return (
    <span
      style={{
        display: "inline-block",
        padding: "3px 10px",
        borderRadius: 999,
        backgroundColor: palette.bg,
        color: palette.fg,
        fontSize: 11,
        fontWeight: 700,
        textTransform: "uppercase",
        letterSpacing: 0.5,
      }}
    >
      {label}
    </span>
  );
}

function MissingAttributesRow({ attrs }: { attrs: string[] }) {
  if (attrs.length === 0) return null;
  return (
    <div style={{ marginTop: 8, display: "flex", flexWrap: "wrap", alignItems: "center", gap: 6 }}>
      <span style={{ fontSize: 11, color: "#475569", fontWeight: 600 }}>Missing:</span>
      {attrs.map((name) => (
        <span
          key={name}
          style={{
            padding: "2px 8px",
            borderRadius: 4,
            backgroundColor: "#fee2e2",
            color: "#991b1b",
            fontSize: 11,
            fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
          }}
        >
          {name}
        </span>
      ))}
    </div>
  );
}

function LifecyclePill({ state }: { state: string }) {
  const palette = LIFECYCLE_COLORS[state] || LIFECYCLE_COLORS.draft;
  const label = LIFECYCLE_LABELS[state] || state;
  return (
    <span
      style={{
        display: "inline-block",
        padding: "2px 8px",
        borderRadius: 999,
        backgroundColor: palette.bg,
        color: palette.fg,
        fontSize: 11,
        fontWeight: 600,
        textTransform: "uppercase",
        letterSpacing: 0.4,
      }}
    >
      {label}
    </span>
  );
}

function viewProductHref(p: SimilarProduct): string | null {
  // Published / engineered products have a dprod URI and live in the marketplace.
  if (p.dp_uri && p.dp_uri.startsWith("dprod:")) {
    return `/product/marketplace/${encodeURIComponent(p.dp_uri)}`;
  }
  // Pre-publish drafts: deep-link into the wizard's edit mode if we know the
  // project — works for owners (and for anyone with the link in this single-user
  // demo). When project_id is missing the row stays read-only.
  if (p.project_id) {
    return `/product/edit/${p.project_id}`;
  }
  return null;
}

export interface DiscoveryPanelProps {
  domain: string;
  idea: string;
  result: DiscoveryResult;
  onUseTemplate: (template: MatchingTemplate) => void;
  onStartFresh: () => void;
  onCancel: () => void;
}

export default function DiscoveryPanel({
  domain,
  idea,
  result,
  onUseTemplate,
  onStartFresh,
  onCancel,
}: DiscoveryPanelProps) {
  const hasProducts = result.similar_products.length > 0;
  const hasTemplates = result.matching_templates.length > 0;

  return (
    <div
      style={{
        padding: 28,
        borderRadius: 12,
        backgroundColor: "#fff",
        border: `1px solid ${productTheme.accent}`,
        boxShadow: "0 6px 18px rgba(15, 23, 42, 0.08)",
      }}
    >
      <div style={{ marginBottom: 18 }}>
        <div style={{ fontSize: 18, fontWeight: 700, color: "#0f172a", marginBottom: 4 }}>
          Before you start authoring…
        </div>
        <div style={{ fontSize: 13, color: "#475569", lineHeight: 1.5 }}>
          We checked the <span style={{ textTransform: "capitalize" }}>{domain}</span> domain
          for similar work and reusable templates. Pick a starting point or continue with a
          fresh schema.
        </div>
        <div
          style={{
            marginTop: 10,
            padding: 10,
            borderRadius: 8,
            backgroundColor: "#f8fafc",
            fontSize: 13,
            color: "#0f172a",
            fontStyle: "italic",
            borderLeft: `3px solid ${productTheme.accent}`,
          }}
        >
          “{idea}”
        </div>
      </div>

      {result.error && (
        <div
          style={{
            padding: 10,
            marginBottom: 16,
            borderRadius: 8,
            backgroundColor: "#fef3c7",
            color: "#92400e",
            fontSize: 12,
          }}
        >
          {result.error}
        </div>
      )}

      {hasProducts && (
        <Section
          title="Similar existing products"
          subtitle="Another team may already be solving this. Have a look before you author a duplicate."
        >
          {result.similar_products.map((p) => {
            const href = viewProductHref(p);
            return (
              <Card key={p.uri}>
                <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "center" }}>
                  <div style={{ fontWeight: 700, color: "#0f172a", fontSize: 14 }}>
                    {p.name || "(untitled)"}
                  </div>
                  <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
                    <VerdictBadge verdict={p.verdict} />
                    <LifecyclePill state={p.lifecycle_state} />
                  </div>
                </div>
                <div
                  style={{ fontSize: 11, color: "#64748b", marginTop: 4 }}
                  title="Share of your intended columns semantically covered by this product's columns (prefix- and synonym-aware) — distinct from the intent-match verdict above."
                >
                  {p.overlap_pct}% of your intended columns{" "}
                  <span style={{ color: "#94a3b8" }}>(semantic)</span> · {p.column_count} columns total
                  {p.owner_email ? ` · owned by ${p.owner_email}` : ""}
                </div>
                {p.delta && (
                  <div style={{ fontSize: 13, color: "#475569", marginTop: 8, lineHeight: 1.45 }}>
                    {p.delta}
                  </div>
                )}
                <MissingAttributesRow attrs={p.missing_attributes} />
                <div style={{ marginTop: 10, display: "flex", gap: 8 }}>
                  {href ? (
                    <a
                      href={href}
                      target="_blank"
                      rel="noopener noreferrer"
                      style={{
                        padding: "6px 12px",
                        fontSize: 12,
                        color: productTheme.accent,
                        backgroundColor: "#fff",
                        border: `1px solid ${productTheme.accent}`,
                        borderRadius: 6,
                        textDecoration: "none",
                        fontWeight: 600,
                      }}
                    >
                      View product →
                    </a>
                  ) : (
                    <span style={{ fontSize: 11, color: "#94a3b8", fontStyle: "italic" }}>
                      No deep link available — ask the owner for context.
                    </span>
                  )}
                </div>
              </Card>
            );
          })}
        </Section>
      )}

      {hasTemplates && (
        <Section
          title="Matching templates"
          subtitle="Start from a curated blueprint instead of the catalog. Edit anything once you're in."
        >
          {result.matching_templates.map((t) => (
            <Card key={t.template_id}>
              <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "center" }}>
                <div style={{ fontWeight: 700, color: "#0f172a", fontSize: 14 }}>
                  {t.name || t.template_id}
                </div>
                <VerdictBadge verdict={t.verdict} />
              </div>
              <div
                style={{ fontSize: 11, color: "#64748b", marginTop: 4 }}
                title="Share of your intended columns semantically covered by this template's columns (prefix- and synonym-aware)."
              >
                {t.overlap_pct}% of your intended columns{" "}
                <span style={{ color: "#94a3b8" }}>(semantic)</span> · {t.column_count} columns total
              </div>
              {t.delta && (
                <div style={{ fontSize: 13, color: "#475569", marginTop: 8, lineHeight: 1.45 }}>
                  {t.delta}
                </div>
              )}
              <MissingAttributesRow attrs={t.missing_attributes} />
              <div style={{ marginTop: 10 }}>
                <button
                  type="button"
                  onClick={() => onUseTemplate(t)}
                  style={{
                    padding: "6px 12px",
                    fontSize: 12,
                    color: "#fff",
                    backgroundColor: productTheme.accent,
                    border: "none",
                    borderRadius: 6,
                    cursor: "pointer",
                    fontWeight: 600,
                  }}
                >
                  Start from this template
                </button>
              </div>
            </Card>
          ))}
        </Section>
      )}

      <Section
        title="Or start fresh"
        subtitle={
          result.recommended_columns.length > 0
            ? `We picked ${result.recommended_columns.length} columns from the ${domain} catalog matched to your idea.`
            : "We couldn't suggest a starter set — pick what you need from the catalog on the next step."
        }
      >
        {result.recommended_columns.length > 0 && (
          <Card>
            <div style={{ fontSize: 12, color: "#475569", lineHeight: 1.5 }}>
              <strong style={{ color: "#0f172a" }}>Recommended columns:</strong>{" "}
              {result.recommended_columns.join(", ")}
            </div>
            {result.rationale && (
              <div style={{ marginTop: 8, fontSize: 12, color: "#64748b", fontStyle: "italic" }}>
                {result.rationale}
              </div>
            )}
          </Card>
        )}
        <div style={{ marginTop: 10 }}>
          <button
            type="button"
            onClick={onStartFresh}
            style={{
              padding: "8px 14px",
              fontSize: 13,
              color: "#fff",
              backgroundColor: productTheme.accent,
              border: "none",
              borderRadius: 6,
              cursor: "pointer",
              fontWeight: 600,
            }}
          >
            Continue with these columns →
          </button>
          <button
            type="button"
            onClick={onCancel}
            style={{
              marginLeft: 10,
              padding: "8px 14px",
              fontSize: 13,
              color: "#475569",
              backgroundColor: "transparent",
              border: "1px solid #cbd5e1",
              borderRadius: 6,
              cursor: "pointer",
            }}
          >
            Back to Step 1
          </button>
        </div>
      </Section>
    </div>
  );
}

function Section({ title, subtitle, children }: { title: string; subtitle?: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 24 }}>
      <div style={{ fontSize: 16, fontWeight: 700, color: "#0f172a", marginBottom: 6 }}>{title}</div>
      {subtitle && (
        <div style={{ fontSize: 13, color: "#64748b", marginBottom: 12, lineHeight: 1.5 }}>{subtitle}</div>
      )}
      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>{children}</div>
    </div>
  );
}

function Card({ children }: { children: React.ReactNode }) {
  return (
    <div
      style={{
        padding: 12,
        borderRadius: 8,
        backgroundColor: "#fafbfc",
        border: "1px solid #e2e8f0",
      }}
    >
      {children}
    </div>
  );
}
