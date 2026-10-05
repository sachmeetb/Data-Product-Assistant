import { Link } from "react-router-dom";
import { productTheme } from "../../theme";

/**
 * Phase 1 stub. Phase 2 replaces most tiles with real destinations
 * (MyProductsDashboard, NewProductWizard, IngestExistingProductPage).
 */
export default function ProductLandingPage() {
  return (
    <div style={{ maxWidth: 960, margin: "0 auto" }}>
      <h1 style={{ fontSize: 24, fontWeight: 700, color: "#0f172a", marginBottom: 8 }}>
        Welcome to the Product Workbench
      </h1>
      <p style={{ fontSize: 14, color: "#475569", marginBottom: 24 }}>
        Browse, propose, and govern data products. Engineering picks up what you submit.
      </p>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))", gap: 20, alignItems: "stretch" }}>
        <Tile
          to="/product/marketplace"
          title="Browse Marketplace"
          description="Discover published data products across the organisation."
          active
        />
        <Tile
          to="/product/my-products"
          title="My Products"
          description="Drafts, submitted requests, and published products you own."
          active
        />
        <Tile
          to="/product/new/source"
          title="Request a Source-aligned Product"
          description="Mirror an operational source close to 1:1. Engineer profiles the database; you validate names, descriptions, and rules."
          active
          accent="source"
        />
        <Tile
          to="/product/new/consumer"
          title="Request a Derived Product"
          description="Compose a product from already-published products (source, aggregate, or consumer). You shape the schema and choose whether it's a reusable Aggregate or a fit-for-purpose Consumer product; the engineer maps it to the inputs you pick."
          active
          accent="derived"
        />
        <Tile
          to="/product/ingest"
          title="Ingest an Existing Product"
          description="Register a product your organisation already operates but hasn't documented."
          active
        />
        <Tile
          to="/product/templates"
          title="Data Product Templates"
          description="Browse, clone, and publish reusable data-product spec templates. Published templates feed the feasibility scan, the derived-product wizard, and Pulse discovery."
          active
        />
        <Tile
          to="/product/discovery"
          title="Discovery"
          description="Assess your data estate bottom-up with a Pulse-powered discovery — inventory existing sources and surface candidate data products."
          active
        />
        <Tile
          to="/product/estate"
          title="Connected Estate"
          description="Scan a live platform directly to inventory what data your organisation actually has."
          active
        />
        <Tile
          to="/product/feasibility"
          title="Data-Product Feasibility"
          description="See which desired reference data products are buildable now — ready, adaptable, assemblable, or absent — against your connected estate."
          active
        />
        <Tile
          to="/product/intake"
          title="Modernization Intake"
          description="Review portfolios an external assessment tool pushed in, then approve to scaffold candidate projects."
          active
        />
        <Tile
          to="/product/semantic-recommender"
          title="Semantic Explorer"
          description="Poke around your data's meaning — extract cross-product business concepts, test ideas with marketplace Semantic Q&A, and review consumer-reported gaps. A space to experiment, not a production semantic layer."
          active
        />
      </div>
    </div>
  );
}

interface TileProps {
  to: string;
  title: string;
  description: string;
  active?: boolean;
  /** Optional kind chip — visually mirrors the marketplace's kind badge. Only
   *  the Request tiles set this; everything else stays neutral. "derived"
   *  covers the contract-first family (aggregate + consumer), whose specific
   *  kind is chosen inside the wizard. */
  accent?: "source" | "derived";
}

const TILE_ACCENT: Record<"source" | "derived", { bg: string; fg: string; border: string; label: string }> = {
  source:  { bg: "#dbeafe", fg: "#1d4ed8", border: "#93c5fd", label: "Source" },
  derived: { bg: "#ede9fe", fg: "#6d28d9", border: "#c4b5fd", label: "Derived" },
};

function Tile({ to, title, description, active = false, accent }: TileProps) {
  const accentDef = accent ? TILE_ACCENT[accent] : null;
  const body = (
    <div
      style={{
        padding: 20,
        borderRadius: 10,
        border: `1px solid ${active ? productTheme.accent : "#e2e8f0"}`,
        backgroundColor: "#ffffff",
        height: "100%",
        opacity: active ? 1 : 0.7,
        cursor: active ? "pointer" : "not-allowed",
        display: "flex",
        flexDirection: "column",
        gap: 8,
        boxSizing: "border-box",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <div style={{ fontSize: 15, fontWeight: 700, color: "#0f172a" }}>{title}</div>
          {accentDef && (
            <span
              style={{
                padding: "2px 8px",
                borderRadius: 999,
                fontSize: 10,
                fontWeight: 700,
                backgroundColor: accentDef.bg,
                color: accentDef.fg,
                border: `1px solid ${accentDef.border}`,
              }}
            >
              {accentDef.label}
            </span>
          )}
        </div>
        {!active && (
          <span style={{ fontSize: 10, fontWeight: 700, letterSpacing: 0.4, textTransform: "uppercase", color: "#94a3b8" }}>
            Coming soon
          </span>
        )}
      </div>
      <div style={{ fontSize: 13, color: "#475569", lineHeight: 1.5 }}>{description}</div>
    </div>
  );
  return active ? (
    <Link to={to} style={{ textDecoration: "none", display: "block" }}>
      {body}
    </Link>
  ) : (
    <div>{body}</div>
  );
}
